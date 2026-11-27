//! Decode probe on every worker (S5.6).
//!
//! Placement needs each node's real decode speed. A memory-copy figure overstates CPU decoding (S4.7), so
//! every agent that runs a worker times real decoding of the catalog's reference model with llama-bench.
//!
//! - llama-bench: `--decode-probe-bench`, or else `llama-bench` beside the worker binary (llama.cpp builds and
//!   the installers put them together).
//! - The model: `--decode-probe-model`, or else the reference model the coordinator offers in its heartbeat
//!   reply. The agent downloads it once over the pinned control channel into `<cache>/probe/`, checks its
//!   SHA-256, and reuses it on later starts. Nothing else is installed (D-07).
//!
//! The probe runs on its own thread, so a download never delays heartbeats. It waits until the machine is
//! quiet (nobody at it, no deployment assigned, contribution not paused) so the benchmark never competes with
//! the owner or with real work (D-12). The result goes into the next heartbeat's capabilities.

use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tracing::{info, warn};

/// The reference model the coordinator offers for the probe (heartbeat reply `decode_probe_model`).
#[derive(Clone, Debug, PartialEq, serde::Deserialize)]
pub struct Offer {
    pub catalog: String,
    pub file: String,
    pub sha256: String,
    pub nbytes: u64,
    /// Path on the coordinator, fetched with the device token over the pinned channel.
    pub url: String,
}

pub struct Source {
    pub coordinator: String,
    pub device_token: String,
    pub fingerprint: Option<String>,
}

pub struct Prober {
    bench: Option<PathBuf>,
    model: Option<(PathBuf, String)>,
    threads: Option<u32>,
    cache: PathBuf,
    source: Arc<Source>,
    quiet: Arc<AtomicBool>,
    result: Arc<Mutex<Option<serde_json::Value>>>,
    started: bool,
}

/// llama-bench beside the worker binary, with the platform's executable suffix.
pub fn bench_beside(worker_bin: &Path) -> Option<PathBuf> {
    let name = if cfg!(windows) { "llama-bench.exe" } else { "llama-bench" };
    let p = worker_bin.parent()?.join(name);
    p.is_file().then_some(p)
}

impl Prober {
    /// `bench` and `model` are the explicit flags; `worker_bin` is used to find llama-bench when no flag
    /// names it. Without a worker and without flags there is nothing to probe.
    pub fn new(bench: Option<PathBuf>, model: Option<(PathBuf, String)>, worker_bin: Option<&Path>,
               threads: Option<u32>, cache: PathBuf, source: Source) -> Prober {
        let bench = bench.or_else(|| worker_bin.and_then(bench_beside));
        if worker_bin.is_some() && bench.is_none() {
            warn!(error_code = "PROBE_FAILED",
                  "decode probe unavailable: no llama-bench beside the worker binary; planning uses the memory-copy \
                   figure");
        }
        Prober { bench, model, threads, cache, source: Arc::new(source), quiet: Arc::new(AtomicBool::new(false)),
                 result: Arc::new(Mutex::new(None)), started: false }
    }

    /// The probe result for the capabilities, once there is one.
    pub fn result(&self) -> Option<serde_json::Value> {
        self.result.lock().unwrap().clone()
    }

    /// Called every loop iteration: whether the machine is quiet enough to benchmark right now.
    pub fn set_quiet(&self, quiet: bool) {
        self.quiet.store(quiet, Ordering::Relaxed);
    }

    /// Start the probe once: at once when the model was named on the command line, otherwise when the
    /// coordinator first offers a reference model.
    pub fn offer(&mut self, offer: Option<&Offer>) {
        if self.started {
            return;
        }
        let Some(bench) = self.bench.clone() else { return };
        let job = match (&self.model, offer) {
            (Some((path, catalog)), _) => Job::Local(path.clone(), catalog.clone()),
            (None, Some(o)) => Job::Fetch(o.clone()),
            (None, None) => return,
        };
        self.started = true;
        let (threads, cache, source) = (self.threads, self.cache.clone(), self.source.clone());
        let (quiet, result) = (self.quiet.clone(), self.result.clone());
        std::thread::Builder::new().name("decode-probe".into()).spawn(move || {
            let (model, catalog) = match job {
                Job::Local(p, c) => (p, c),
                Job::Fetch(o) => match fetch(&cache, &source, &o) {
                    Ok(p) => (p, o.catalog),
                    Err(e) => {
                        warn!(error_code = "PROBE_FAILED", "decode probe unavailable: {e}");
                        return;
                    }
                },
            };
            while !quiet.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_secs(5));
            }
            let backend = if threads.is_some() { "cpu" } else { "default" };
            match crate::probe::decode_probe(&bench, &model, threads) {
                Ok(tps) => {
                    info!(tokens_per_s = tps, "decode probe: {tps:.1} tokens/s on {backend} ({catalog})");
                    *result.lock().unwrap() = Some(serde_json::json!({
                        "model": catalog, "tokens_per_s": tps, "backend": backend, "threads": threads}));
                }
                Err(e) => warn!(error_code = "PROBE_FAILED", "decode probe unavailable: {e}"),
            }
        }).expect("cannot start the decode-probe thread");
    }
}

enum Job {
    Local(PathBuf, String),
    Fetch(Offer),
}

/// The offered model in `<cache>/probe/`, downloading and verifying it when it is missing or wrong.
fn fetch(cache: &Path, source: &Source, o: &Offer) -> Result<PathBuf, String> {
    if o.file.contains(['/', '\\']) || o.file.starts_with('.') || !o.url.starts_with('/') {
        return Err(format!("refusing the offered probe model {:?}", o.file));
    }
    let dir = cache.join("probe");
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    let path = dir.join(&o.file);
    let marker = dir.join(format!("{}.sha256", o.file));
    if std::fs::metadata(&path).is_ok_and(|m| m.len() == o.nbytes)
        && std::fs::read_to_string(&marker).is_ok_and(|s| s.trim() == o.sha256) {
        return Ok(path);
    }
    info!("downloading the decode-probe model {} ({:.0} MB)", o.file, o.nbytes as f64 / 1e6);
    let url = format!("{}{}", source.coordinator.trim_end_matches('/'), o.url);
    let http = crate::control::client(&source.coordinator, source.fingerprint.as_deref(), Duration::from_secs(15),
                                      Duration::from_secs(6 * 3600))?.0;
    let mut resp = http.get(&url).bearer_auth(&source.device_token).send()
        .map_err(|e| format!("probe model download failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("the coordinator refused the probe model ({})", resp.status()));
    }
    let part = dir.join(format!(".{}.part", o.file));
    let digest = copy_verified(&mut resp, &part, o.nbytes)?;
    if digest != o.sha256 {
        let _ = std::fs::remove_file(&part);
        return Err(format!("probe model failed verification (sha256 {digest})"));
    }
    std::fs::rename(&part, &path).map_err(|e| e.to_string())?;
    std::fs::write(&marker, &o.sha256).map_err(|e| e.to_string())?;
    Ok(path)
}

/// Copy at most `nbytes` from `src` into `part`, returning the SHA-256. More or fewer bytes is an error.
fn copy_verified(src: &mut dyn Read, part: &Path, nbytes: u64) -> Result<String, String> {
    let mut out = std::fs::File::create(part).map_err(|e| format!("cannot write {}: {e}", part.display()))?;
    let (mut sha, mut got, mut buf) = (Sha256::new(), 0u64, vec![0u8; 1 << 20]);
    let fail = |msg: String| {
        let _ = std::fs::remove_file(part);
        Err(msg)
    };
    loop {
        let n = match src.read(&mut buf) {
            Ok(n) => n,
            Err(e) => return fail(format!("probe model download interrupted: {e}")),
        };
        if n == 0 {
            break;
        }
        got += n as u64;
        if got > nbytes {
            return fail("the coordinator sent more bytes than the probe model has".into());
        }
        sha.update(&buf[..n]);
        if let Err(e) = out.write_all(&buf[..n]) {
            return fail(format!("cannot write the probe model: {e}"));
        }
    }
    if got != nbytes {
        return fail(format!("probe model download ended early ({got} of {nbytes} bytes)"));
    }
    Ok(sha.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("idlemesh-decode-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn copies_and_hashes_exactly_the_expected_bytes() {
        let part = tmp("ok").join(".m.part");
        let digest = copy_verified(&mut &b"abc"[..], &part, 3).unwrap();
        assert_eq!(digest, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"); // sha256("abc")
        assert_eq!(std::fs::read(&part).unwrap(), b"abc");
    }

    #[test]
    fn too_many_or_too_few_bytes_are_refused_and_cleaned_up() {
        let part = tmp("bad").join(".m.part");
        assert!(copy_verified(&mut &b"abcd"[..], &part, 3).is_err());
        assert!(!part.exists());
        assert!(copy_verified(&mut &b"ab"[..], &part, 3).is_err());
        assert!(!part.exists());
    }

    #[test]
    fn offered_file_names_cannot_leave_the_probe_directory() {
        let src = Source { coordinator: "https://127.0.0.1:1".into(), device_token: "t".into(), fingerprint: None };
        for file in ["../x.gguf", "a/b.gguf", "a\\b.gguf", ".hidden"] {
            let o = Offer { catalog: "c".into(), file: file.into(), sha256: "0".repeat(64), nbytes: 1,
                            url: "/api/v1/x".into() };
            assert!(fetch(&tmp("names"), &src, &o).unwrap_err().starts_with("refusing"));
        }
        let o = Offer { catalog: "c".into(), file: "m.gguf".into(), sha256: "0".repeat(64), nbytes: 1,
                        url: "https://elsewhere.example/m.gguf".into() };
        assert!(fetch(&tmp("names"), &src, &o).unwrap_err().starts_with("refusing"));
    }

    #[test]
    fn a_verified_cached_model_is_reused_without_a_download() {
        let cache = tmp("cached");
        std::fs::create_dir_all(cache.join("probe")).unwrap();
        std::fs::write(cache.join("probe/m.gguf"), b"abc").unwrap();
        let sha = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        std::fs::write(cache.join("probe/m.gguf.sha256"), sha).unwrap();
        // The coordinator address is unreachable, so success proves no download was tried.
        let src = Source { coordinator: "https://127.0.0.1:1".into(), device_token: "t".into(), fingerprint: None };
        let o = Offer { catalog: "c".into(), file: "m.gguf".into(), sha256: sha.into(), nbytes: 3,
                        url: "/api/v1/x".into() };
        assert_eq!(fetch(&cache, &src, &o).unwrap(), cache.join("probe/m.gguf"));
    }

    #[test]
    fn llama_bench_is_found_beside_the_worker() {
        let dir = tmp("bench");
        let worker = dir.join(if cfg!(windows) { "ggml-rpc-server.exe" } else { "ggml-rpc-server" });
        assert_eq!(bench_beside(&worker), None);
        let bench = dir.join(if cfg!(windows) { "llama-bench.exe" } else { "llama-bench" });
        std::fs::write(&bench, b"").unwrap();
        assert_eq!(bench_beside(&worker), Some(bench));
    }
}
