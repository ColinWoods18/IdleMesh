//! Remote diagnosis on the agent side (D-11): crash reports (S4.3) and diagnostic bundles (S4.4).
//!
//! - **Agent panics:** a panic hook writes a crash report (message, location, backtrace, version, build hash, OS,
//!   redacted config) to `crashes/` next to the state file before the process ends. It is uploaded on the next
//!   start, once the coordinator is reachable.
//! - **Worker crashes:** when the llama.cpp worker exits on its own, the agent writes a report with its exit status
//!   and last output lines, and uploads it with the next heartbeat.
//! - **Bundles:** when a heartbeat reply asks for one, the agent packages recent logs, versions, probes, health and
//!   redacted config, and uploads it.
//!
//! Everything needs the owner's diagnostics consent: without it reports stay on the node and nothing is sent.
//! Text passes through `logging::redact` and the config never holds the device token, keys or enrollment code.

use crate::logging;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use tracing::{error, info, warn};

pub const BUILD_HASH: &str = env!("IDLEMESH_BUILD_HASH");
pub const CRASH_DIR: &str = "crashes";
/// Crash reports kept on disk while the coordinator is unreachable or consent is missing (oldest go first).
const MAX_SAVED: usize = 50;
/// Log tail in a bundle; the whole bundle must stay under the coordinator's 1 MiB request limit.
const BUNDLE_LOG_BYTES: usize = 500_000;
const BUNDLE_MAX_BYTES: usize = 950_000;

struct Context {
    dir: PathBuf,
    config: Value,
}

static CTX: OnceLock<Mutex<Context>> = OnceLock::new();

pub fn versions() -> Value {
    json!({"agent": env!("CARGO_PKG_VERSION"), "build_hash": BUILD_HASH, "os": std::env::consts::OS,
           "arch": std::env::consts::ARCH})
}

/// Remember where crash reports go and the redacted config they carry, and install the panic hook.
pub fn install(state_dir: &Path, config: Value) {
    let ctx = Context { dir: state_dir.join(CRASH_DIR), config };
    if CTX.set(Mutex::new(ctx)).is_err() {
        return;
    }
    let default = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let message = info.payload().downcast_ref::<&str>().map(|s| s.to_string())
            .or_else(|| info.payload().downcast_ref::<String>().cloned()).unwrap_or_else(|| "panic".into());
        let location = info.location().map(|l| format!("{}:{}:{}", l.file(), l.line(), l.column()));
        let backtrace = std::backtrace::Backtrace::force_capture().to_string();
        let path = save("agent", &message, location.as_deref(), Some(&backtrace), None, &[]);
        error!(error_code = "AGENT_PANIC", location = location.as_deref().unwrap_or("unknown"),
               "the agent crashed: {message} (crash report saved to {})",
               path.as_ref().map_or("nowhere".into(), |p| p.display().to_string()));
        default(info);
    }));
}

/// Save a worker crash: its exit status and last output lines.
pub fn save_worker_crash(exit_status: &str, tail: &[String]) -> Option<PathBuf> {
    let message = tail.iter().rev().find(|l| !l.trim().is_empty()).cloned().unwrap_or_default();
    let path = save("worker", &message, None, None, Some(exit_status), tail);
    warn!(error_code = "WORKER_CRASHED", exit = exit_status, "worker crash report saved");
    path
}

fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let mut end = max;
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    s[..end].to_string()
}

fn save(kind: &str, message: &str, location: Option<&str>, backtrace: Option<&str>, exit_status: Option<&str>,
        tail: &[String]) -> Option<PathBuf> {
    let ctx = CTX.get()?.lock().unwrap_or_else(|e| e.into_inner());
    let id = logging::new_trace_id();
    let v = versions();
    let report = json!({
        "crash_id": id, "kind": kind, "ts": logging::rfc3339(std::time::SystemTime::now()),
        "version": v["agent"], "build_hash": v["build_hash"], "os": v["os"], "arch": v["arch"],
        "message": truncate(&logging::redact(message), 4000),
        "location": location.map(logging::redact),
        "backtrace": backtrace.map(|b| truncate(&logging::redact(b), 30_000)),
        "exit_status": exit_status,
        "log_tail": tail.iter().rev().take(200).rev().map(|l| truncate(&logging::redact(l), 600)).collect::<Vec<_>>(),
        "config": ctx.config,
    });
    std::fs::create_dir_all(&ctx.dir).ok()?;
    let path = ctx.dir.join(format!("crash-{}-{id}.json", logging::rfc3339(std::time::SystemTime::now())
        .replace([':', '.', '+'], "-")));
    crate::secure_fs::write_private(&path, report.to_string().as_bytes()).ok()?;
    let mut saved = list_in(&ctx.dir);
    while saved.len() > MAX_SAVED {
        let _ = std::fs::remove_file(saved.remove(0));
    }
    Some(path)
}

fn list_in(dir: &Path) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir).map(|rd| rd.flatten().map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json")).collect()).unwrap_or_default();
    v.sort();
    v
}

/// Saved crash reports not yet accepted by the coordinator, oldest first.
pub fn pending() -> Vec<PathBuf> {
    CTX.get().map(|c| list_in(&c.lock().unwrap_or_else(|e| e.into_inner()).dir)).unwrap_or_default()
}

/// Upload saved crash reports. A report the coordinator accepts (or calls invalid) is deleted; anything else stays
/// for the next try. Returns how many were delivered.
pub fn upload_pending(http: &reqwest::blocking::Client, base: &str, device_id: &str, token: &str) -> usize {
    let url = format!("{}/api/v1/devices/{device_id}/crash-reports", base.trim_end_matches('/'));
    let mut sent = 0;
    for path in pending().into_iter().take(5) {
        let Ok(body) = std::fs::read(&path) else { continue };
        let resp = http.post(&url).bearer_auth(token).header("X-Trace-Id", logging::new_trace_id())
            .header("Content-Type", "application/json").body(body).send();
        match resp.map(|r| r.status().as_u16()) {
            Ok(200..=299) => {
                let _ = std::fs::remove_file(&path);
                sent += 1;
            }
            Ok(400 | 413 | 422) => {
                error!(error_code = "CRASH_REPORT_UPLOAD_FAILED", "the coordinator refused a crash report as \
                       invalid; deleting it: {}", path.display());
                let _ = std::fs::remove_file(&path);
            }
            Ok(status) => {
                warn!(error_code = "CRASH_REPORT_UPLOAD_FAILED", status, "crash report not uploaded; kept for later");
                break;
            }
            Err(e) => {
                warn!(error_code = "CRASH_REPORT_UPLOAD_FAILED", "crash report not uploaded; kept for later: {e}");
                break;
            }
        }
    }
    if sent > 0 {
        info!(reports = sent, "uploaded {sent} crash report(s)");
    }
    sent
}

/// A diagnostic bundle: versions plus whatever the caller knows now (probe, hardware, health, worker, telemetry,
/// redacted config), the saved crash reports, and as much of the log tail as fits under the size limit.
pub fn bundle(sections: Value) -> Value {
    let crashes: Vec<Value> = pending().iter().filter_map(|p| std::fs::read(p).ok())
        .filter_map(|b| serde_json::from_slice::<Value>(&b).ok())
        .map(|c| json!({"crash_id": c["crash_id"], "kind": c["kind"], "ts": c["ts"], "message": c["message"]}))
        .collect();
    let config = CTX.get().map(|c| c.lock().unwrap_or_else(|e| e.into_inner()).config.clone()).unwrap_or_default();
    let mut b = json!({"versions": versions(), "created_at": logging::rfc3339(std::time::SystemTime::now()),
                       "config": config, "pending_crash_reports": crashes,
                       "log_level": logging::level().map(|l| l.to_string())});
    if let (Some(obj), Some(extra)) = (b.as_object_mut(), sections.as_object()) {
        for (k, v) in extra {
            obj.insert(k.clone(), v.clone());
        }
    }
    let mut lines = logging::tail(BUNDLE_LOG_BYTES);
    loop {
        b["logs"] = json!({"file": logging::LOG_FILE, "lines": lines});
        if b.to_string().len() <= BUNDLE_MAX_BYTES || lines.is_empty() {
            return b;
        }
        let drop = (lines.len() / 4).max(1);
        lines.drain(..drop); // escaping made it bigger than the raw bytes: keep the newest lines
    }
}

/// Build and upload the bundle for one request. Returns an error description if it could not be delivered.
pub fn send_bundle(http: &reqwest::blocking::Client, base: &str, device_id: &str, token: &str, bundle_id: &str,
                   bundle: &Value) -> Result<(), String> {
    let url = format!("{}/api/v1/devices/{device_id}/diagnostics/{bundle_id}", base.trim_end_matches('/'));
    let resp = http.post(url).bearer_auth(token).header("X-Trace-Id", logging::new_trace_id()).json(bundle).send()
        .map_err(|e| e.to_string())?;
    if resp.status().is_success() {
        Ok(())
    } else {
        Err(format!("{}: {}", resp.status(), resp.text().unwrap_or_default()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crash_reports_are_saved_redacted_and_bounded() {
        let dir = std::env::temp_dir().join(format!("idlemesh-diag-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        install(&dir, json!({"name": "test node", "consent": {"diagnostics": true}}));
        let p = save_worker_crash("exit code: 139", &["loading model".into(),
            "Authorization: Bearer s3cret-T0ken.value".into(), "ggml_abort: out of memory".into()]).unwrap();
        let v: Value = serde_json::from_slice(&std::fs::read(&p).unwrap()).unwrap();
        assert_eq!(v["kind"], "worker");
        assert_eq!(v["message"], "ggml_abort: out of memory");
        assert_eq!(v["exit_status"], "exit code: 139");
        assert!(!v.to_string().contains("s3cret-T0ken"), "{v}");
        assert_eq!(v["config"]["name"], "test node");
        assert!(v["crash_id"].as_str().unwrap().len() >= 8);
        assert_eq!(pending(), vec![p.clone()]);
        // A bundle lists it and carries versions.
        let b = bundle(json!({"health": {"cpu_util": 0.5}}));
        assert_eq!(b["versions"]["build_hash"], BUILD_HASH);
        assert_eq!(b["pending_crash_reports"][0]["kind"], "worker");
        assert_eq!(b["health"]["cpu_util"], 0.5);
        assert!(b.to_string().len() <= BUNDLE_MAX_BYTES);
        // Old reports are dropped beyond the limit.
        for _ in 0..(MAX_SAVED + 5) {
            save_worker_crash("exit code: 1", &[]);
        }
        assert_eq!(pending().len(), MAX_SAVED);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn truncation_respects_character_boundaries() {
        assert_eq!(truncate("héllo", 2), "h");
        assert_eq!(truncate("abc", 10), "abc");
    }
}
