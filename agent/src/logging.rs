//! Structured logging for the agent (D-11, sprint 1).
//!
//! Two outputs, both fed by the `tracing` macros used throughout the agent:
//! - a **JSON lines file** (`idlemesh-agent.log`, rotated by size, a fixed number of old files kept)
//!   in the LogEvent shape from PROJECT_PLAN §6, for remote diagnosis and later upload;
//! - the **console**, with just the message, so people running the agent by hand (and the existing
//!   integration tests) see the same text as before.
//!
//! Correlation IDs come from a process-wide context: the agent serves one node, its current deployment
//! and the requests the coordinator says are running on it (heartbeat replies list them). Every line
//! carries them, including lines from the tunnel and prefetch threads and the captured worker output.
//! An event may also set `trace_id`, `deployment_id`, `request_id` or `error_code` itself.
//!
//! Every finished line passes through [`redact`] before it is written (S1.9).

use regex::Regex;
use serde_json::{json, Map, Value};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock, RwLock};
use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::layer::{Context, SubscriberExt};
use tracing_subscriber::{filter::LevelFilter, Layer};

pub const COMPONENT: &str = "agent";
pub const LOG_FILE: &str = "idlemesh-agent.log";
/// Events with this target go to the file only (for example the final error, which is also printed).
pub const FILE_ONLY: &str = "file_only";

// --- correlation context ----------------------------------------------------------------------------

#[derive(Default)]
struct Ids {
    node_id: Option<String>,
    deployment_id: Option<String>,
    request_ids: Vec<String>,
}

static IDS: RwLock<Ids> = RwLock::new(Ids { node_id: None, deployment_id: None, request_ids: Vec::new() });

pub fn set_node(id: &str) {
    IDS.write().unwrap().node_id = Some(id.to_string());
}

/// Returns true when the deployment changed.
pub fn set_deployment(id: Option<&str>) -> bool {
    let mut ids = IDS.write().unwrap();
    let new = id.map(str::to_string);
    let changed = ids.deployment_id != new;
    ids.deployment_id = new;
    changed
}

/// Returns true when the set of active requests changed.
pub fn set_requests(mut ids_in: Vec<String>) -> bool {
    ids_in.sort();
    let mut ids = IDS.write().unwrap();
    let changed = ids.request_ids != ids_in;
    ids.request_ids = ids_in;
    changed
}

/// A short random ID for one agent-to-coordinator exchange, sent as `X-Trace-Id`.
pub fn new_trace_id() -> String {
    use ring::rand::SecureRandom;
    let mut b = [0u8; 8];
    ring::rand::SystemRandom::new().fill(&mut b).expect("system random");
    b.iter().map(|x| format!("{x:02x}")).collect()
}

// --- redaction --------------------------------------------------------------------------------------

const SECRET_KEYS: &[&str] = &["password", "passphrase", "secret", "token", "device_token", "api_key", "apikey",
                               "authorization", "cookie", "csrf", "recovery", "recovery_code", "private_key",
                               "key_pem", "tunnel_key_pem", "code"];
const CONTENT_KEYS: &[&str] = &["prompt", "content", "messages", "message_text", "text", "response", "completion",
                                "reply", "history", "title"];
const REDACTED: &str = "[REDACTED]";

struct Patterns {
    pem: Regex,
    bearer: Regex,
    api_key: Regex,
    json_pair: Regex,
    text_pair: Regex,
    home: Regex,
}

fn patterns() -> &'static Patterns {
    static P: OnceLock<Patterns> = OnceLock::new();
    P.get_or_init(|| {
        let mut keys: Vec<&str> = SECRET_KEYS.iter().chain(CONTENT_KEYS).copied().collect();
        keys.sort_by_key(|k| std::cmp::Reverse(k.len()));
        let alt = keys.join("|");
        Patterns {
            pem: Regex::new(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----").unwrap(),
            bearer: Regex::new(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+").unwrap(),
            api_key: Regex::new(r"\bdifa_[A-Za-z0-9_-]{8,}").unwrap(),
            json_pair: Regex::new(&format!(r#"(?i)("(?:{alt})"\s*:\s*)"(?:[^"\\]|\\.)*""#)).unwrap(),
            // key=value or key: value in free text. The Python version uses a look-behind, which the regex
            // crate lacks, so the preceding character is captured and put back.
            text_pair: Regex::new(&format!(r#"(?i)(^|[^A-Za-z0-9_])((?:{alt})\s*[=:]\s*)[^\s,;)\]}}"'\[]+"#))
                .unwrap(),
            home: Regex::new(r#"(?i)((?:[A-Z]:)?(?:\\\\|\\|/)(?:Users|home)(?:\\\\|\\|/))[^\\/\s"']+"#).unwrap(),
        }
    })
}

/// Remove secrets, prompt and response text, and user names in home paths from one log line.
pub fn redact(text: &str) -> String {
    let p = patterns();
    let t = p.pem.replace_all(text, "[REDACTED_PRIVATE_KEY]");
    let t = p.bearer.replace_all(&t, format!("$1 {REDACTED}"));
    let t = p.api_key.replace_all(&t, format!("difa_{REDACTED}"));
    let t = p.json_pair.replace_all(&t, format!("${{1}}\"{REDACTED}\""));
    let t = p.text_pair.replace_all(&t, format!("${{1}}${{2}}{REDACTED}"));
    let t = p.home.replace_all(&t, "${1}<user>");
    t.into_owned()
}

fn is_private_key(name: &str) -> bool {
    let n = name.to_ascii_lowercase();
    SECRET_KEYS.contains(&n.as_str()) || CONTENT_KEYS.contains(&n.as_str())
}

// --- size-capped rotating file ----------------------------------------------------------------------

pub struct RotatingFile {
    path: PathBuf,
    max_bytes: u64,
    keep: usize,
    file: File,
    size: u64,
}

impl RotatingFile {
    pub fn open(dir: &Path, max_bytes: u64, keep: usize) -> std::io::Result<Self> {
        std::fs::create_dir_all(dir)?;
        let path = dir.join(LOG_FILE);
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        let size = file.metadata()?.len();
        Ok(RotatingFile { path, max_bytes, keep, file, size })
    }

    fn rotated(&self, i: usize) -> PathBuf {
        PathBuf::from(format!("{}.{i}", self.path.display()))
    }

    fn rotate(&mut self) -> std::io::Result<()> {
        let _ = std::fs::remove_file(self.rotated(self.keep));
        for i in (1..self.keep).rev() {
            let _ = std::fs::rename(self.rotated(i), self.rotated(i + 1));
        }
        // Windows cannot rename an open file, so close it first by swapping in the next one.
        let next = self.rotated(1);
        let placeholder = OpenOptions::new().create(true).append(true).open(self.path.with_extension("rotating"))?;
        drop(std::mem::replace(&mut self.file, placeholder));
        if self.keep > 0 {
            std::fs::rename(&self.path, next)?;
        } else {
            std::fs::remove_file(&self.path)?;
        }
        self.file = OpenOptions::new().create(true).append(true).open(&self.path)?;
        let _ = std::fs::remove_file(self.path.with_extension("rotating"));
        self.size = 0;
        Ok(())
    }

    pub fn write_line(&mut self, line: &str) -> std::io::Result<()> {
        let n = line.len() as u64 + 1;
        if self.size > 0 && self.size + n > self.max_bytes {
            self.rotate()?;
        }
        self.file.write_all(line.as_bytes())?;
        self.file.write_all(b"\n")?;
        self.size += n;
        Ok(())
    }
}

// --- layers -----------------------------------------------------------------------------------------

#[derive(Default)]
struct Fields {
    message: String,
    map: Map<String, Value>,
}

impl Visit for Fields {
    fn record_str(&mut self, field: &Field, value: &str) {
        self.put(field, Value::String(value.to_string()));
    }
    fn record_i64(&mut self, field: &Field, value: i64) {
        self.put(field, json!(value));
    }
    fn record_u64(&mut self, field: &Field, value: u64) {
        self.put(field, json!(value));
    }
    fn record_f64(&mut self, field: &Field, value: f64) {
        self.put(field, json!(value));
    }
    fn record_bool(&mut self, field: &Field, value: bool) {
        self.put(field, json!(value));
    }
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        self.put(field, Value::String(format!("{value:?}")));
    }
}

impl Fields {
    fn put(&mut self, field: &Field, v: Value) {
        if field.name() == "message" {
            self.message = v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string());
        } else if is_private_key(field.name()) {
            self.map.insert(field.name().to_string(), Value::String(REDACTED.into()));
        } else {
            self.map.insert(field.name().to_string(), v);
        }
    }
}

const TOP_LEVEL: &[&str] = &["trace_id", "node_id", "deployment_id", "request_id", "error_code"];

/// Build the JSON object for one event (exposed for tests).
fn event_json(level: &Level, target: &str, mut f: Fields) -> Value {
    let mut line = Map::new();
    line.insert("ts".into(), json!(now_rfc3339()));
    line.insert("level".into(), json!(level.as_str().to_ascii_lowercase()));
    line.insert("component".into(), json!(COMPONENT));
    line.insert("logger".into(), json!(target));
    line.insert("message".into(), json!(f.message));
    line.insert("version".into(), json!(env!("CARGO_PKG_VERSION")));
    {
        let ids = IDS.read().unwrap();
        if let Some(n) = &ids.node_id {
            line.insert("node_id".into(), json!(n));
        }
        if let Some(d) = &ids.deployment_id {
            line.insert("deployment_id".into(), json!(d));
        }
        if !ids.request_ids.is_empty() {
            line.insert("request_ids".into(), json!(ids.request_ids));
        }
    }
    for k in TOP_LEVEL {
        if let Some(v) = f.map.remove(*k) {
            line.insert((*k).into(), v);
        }
    }
    if !f.map.is_empty() {
        line.insert("fields".into(), Value::Object(f.map));
    }
    Value::Object(line)
}

fn now_rfc3339() -> String {
    rfc3339(std::time::SystemTime::now())
}

/// UTC with milliseconds, without pulling in a date crate.
pub fn rfc3339(t: std::time::SystemTime) -> String {
    let d = t.duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    let (secs, ms) = (d.as_secs() as i64, d.subsec_millis());
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    let (h, m, s) = (rem / 3600, rem % 3600 / 60, rem % 60);
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}.{ms:03}+00:00")
}

struct JsonFileLayer {
    file: Mutex<RotatingFile>,
}

impl<S: Subscriber> Layer<S> for JsonFileLayer {
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        let mut f = Fields::default();
        event.record(&mut f);
        let meta = event.metadata();
        let line = redact(&event_json(meta.level(), meta.target(), f).to_string());
        if let Ok(mut file) = self.file.lock() {
            let _ = file.write_line(&line);
        }
    }
}

/// Console: the message only (plus a level prefix for warnings and errors), as the agent printed before.
struct ConsoleLayer;

impl<S: Subscriber> Layer<S> for ConsoleLayer {
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        let meta = event.metadata();
        if meta.target() == FILE_ONLY {
            return;
        }
        let mut f = Fields::default();
        event.record(&mut f);
        let prefix = match *meta.level() {
            Level::ERROR => "error: ",
            Level::WARN => "warning: ",
            _ => "",
        };
        let mut out = std::io::stdout().lock();
        let _ = writeln!(out, "{prefix}{}", redact(&f.message));
        let _ = out.flush();
    }
}

pub struct Options {
    pub level: LevelFilter,
    pub dir: Option<PathBuf>,
    pub max_bytes: u64,
    pub keep: usize,
}

pub fn parse_level(s: &str) -> Result<LevelFilter, String> {
    s.parse::<LevelFilter>().map_err(|_| format!("unknown log level {s:?} (use error, warn, info, debug or trace)"))
}

/// Install the console and (when a directory is given) the JSON file outputs. Call once, early.
pub fn init(opts: &Options) -> Result<(), String> {
    let file = match &opts.dir {
        Some(dir) => Some(RotatingFile::open(dir, opts.max_bytes, opts.keep)
            .map_err(|e| format!("cannot open the log file in {}: {e}", dir.display()))?),
        None => None,
    };
    let registry = tracing_subscriber::registry()
        .with(ConsoleLayer.with_filter(opts.level))
        .with(file.map(|f| JsonFileLayer { file: Mutex::new(f) }.with_filter(opts.level)));
    tracing::subscriber::set_global_default(registry).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redact_removes_every_secret_form() {
        let samples = [
            "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n-----END PRIVATE KEY-----",
            "Authorization: Bearer s3cr3t-T0ken_value.abc",
            "used key difa_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
            r#"{"device_token": "tok-123-abc", "passphrase": "correct horse"}"#,
            "enroll code=K7Q2-PLMX retry",
            r#"{"role": "user", "content": "my private question"}"#,
        ];
        for raw in samples {
            let out = redact(raw);
            for needle in ["MIGHAgEA", "s3cr3t-T0ken", "AbCdEfGhIjKl", "tok-123-abc", "correct horse", "K7Q2-PLMX",
                           "my private question"] {
                assert!(!out.contains(needle), "{raw} -> {out}");
            }
        }
    }

    #[test]
    fn redact_hides_user_names_in_home_paths() {
        for raw in [r"C:\Users\alice\.cache\idlemesh", "/home/alice/.cache", "/Users/alice/Library",
                    r#""C:\\Users\\alice\\x""#] {
            assert!(!redact(raw).contains("alice"), "{raw} -> {}", redact(raw));
        }
    }

    #[test]
    fn redact_keeps_ordinary_text() {
        let raw = "heartbeat ok: paused=false power=ac cap=4.00GB rtt=3.1ms error_code=WORKER_LOST";
        assert_eq!(redact(raw), raw);
    }

    #[test]
    fn event_lines_carry_context_ids_and_top_level_fields() {
        set_node("dev_test");
        let mut f = Fields { message: "worker killed".into(), ..Default::default() };
        f.map.insert("error_code".into(), json!("MEMORY_CAP_EXCEEDED"));
        f.map.insert("trace_id".into(), json!("abc"));
        f.map.insert("used_mb".into(), json!(512));
        let v = event_json(&Level::WARN, "idlemesh_agent::worker", f);
        assert_eq!(v["level"], "warn");
        assert_eq!(v["component"], "agent");
        assert_eq!(v["node_id"], "dev_test");
        assert_eq!(v["error_code"], "MEMORY_CAP_EXCEEDED");
        assert_eq!(v["trace_id"], "abc");
        assert_eq!(v["fields"]["used_mb"], 512);
        assert!(v["ts"].as_str().unwrap().ends_with("+00:00"));
    }

    #[test]
    fn timestamps_are_valid_rfc3339() {
        let ts = now_rfc3339();
        assert_eq!(ts.len(), "2026-11-19T10:43:32.123+00:00".len());
        let year: i32 = ts[..4].parse().unwrap();
        assert!((2024..2100).contains(&year));
    }

    #[test]
    fn rotation_caps_size_and_keeps_n_files() {
        let dir = std::env::temp_dir().join(format!("idlemesh-log-rot-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut f = RotatingFile::open(&dir, 1000, 2).unwrap();
        for i in 0..200 {
            f.write_line(&format!("{{\"n\":{i},\"pad\":\"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\"}}")).unwrap();
        }
        drop(f);
        let mut names: Vec<String> = std::fs::read_dir(&dir).unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
        names.sort();
        assert_eq!(names, vec![LOG_FILE.to_string(), format!("{LOG_FILE}.1"), format!("{LOG_FILE}.2")]);
        for n in &names {
            assert!(std::fs::metadata(dir.join(n)).unwrap().len() <= 1000);
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Every error code the agent logs is in the shared catalog (S1.8).
    #[test]
    fn agent_error_codes_are_catalogued() {
        let catalog: Value = serde_json::from_str(include_str!("../../contracts/error-codes.json")).unwrap();
        let known: Vec<&str> = catalog["codes"].as_array().unwrap().iter()
            .map(|c| c["code"].as_str().unwrap()).collect();
        let used = Regex::new(r#"error_code\s*=\s*"([A-Z][A-Z0-9_]+)""#).unwrap();
        let sources = [include_str!("main.rs"), include_str!("worker.rs"), include_str!("tunnel.rs"),
                       include_str!("prefetch.rs"), include_str!("probe.rs"), include_str!("control.rs"),
                       include_str!("idle.rs"), include_str!("telemetry.rs")];
        let mut count = 0;
        for src in sources {
            for c in used.captures_iter(src) {
                count += 1;
                assert!(known.contains(&&c[1]), "{} is not in contracts/error-codes.json", &c[1]);
            }
        }
        assert!(count >= 5, "expected the agent to log catalogued codes, found {count}");
    }
}
