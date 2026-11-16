//! Partition prefetch (FR 30): download this device's share of the model before it loads.
//!
//! The coordinator's manifest lists the byte ranges of the pinned model file this device will hold, each
//! with a SHA-256. The agent fetches each range with an HTTP Range request (from the model's origin, or
//! from the coordinator on a LAN), verifies it, and stores it where llama.cpp's RPC worker looks for
//! cached weights: `<cache>/rpc/<FNV-1a hash as 16 hex digits>`. When llama-server later offers that hash
//! the worker loads the tensor from disk instead of receiving it over the link. Only content that
//! passed SHA-256 verification is ever given a cache name. The owner must have accepted the model's
//! license on this device first.

use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// FNV-1a 64, as ggml-rpc.cpp computes the cache key.
pub struct Fnv(u64);

impl Fnv {
    pub fn new() -> Self {
        Fnv(0xcbf29ce484222325)
    }
    pub fn update(&mut self, data: &[u8]) {
        for b in data {
            self.0 ^= *b as u64;
            self.0 = self.0.wrapping_mul(0x100000001b3);
        }
    }
    pub fn name(&self) -> String {
        format!("{:016x}", self.0)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Status {
    pub deployment_id: String,
    pub state: &'static str,
    pub bytes_done: u64,
    pub bytes_total: u64,
    pub error: Option<String>,
}

impl Status {
    pub fn json(&self) -> serde_json::Value {
        serde_json::json!({"deployment_id": self.deployment_id, "state": self.state, "bytes_done": self.bytes_done,
                           "bytes_total": self.bytes_total, "error": self.error})
    }
}

#[derive(Clone)]
pub struct Source {
    pub coordinator: String,
    pub device_token: String,
    /// Pinned control certificate fingerprint; weights from the coordinator use the same pinned channel.
    pub fingerprint: Option<String>,
}

pub struct Prefetcher {
    cache: PathBuf,
    licenses: Vec<String>,
    source: Source,
    status: Arc<Mutex<Option<Status>>>,
    active: Option<String>,
}

#[derive(Debug, PartialEq)]
struct Item {
    name: String,
    offset: u64,
    nbytes: u64,
    sha256: String,
}

fn parse_items(manifest: &serde_json::Value) -> Result<Vec<Item>, String> {
    let list = manifest["tensors"].as_array().ok_or("manifest has no tensor list")?;
    list.iter()
        .map(|t| {
            Ok(Item {
                name: t["name"].as_str().ok_or("tensor without name")?.to_string(),
                offset: t["offset"].as_u64().ok_or("tensor without offset")?,
                nbytes: t["nbytes"].as_u64().ok_or("tensor without size")?,
                sha256: t["sha256"].as_str().filter(|s| s.len() == 64).ok_or("tensor without sha256")?.to_lowercase(),
            })
        })
        .collect()
}

impl Prefetcher {
    pub fn new(cache: PathBuf, licenses: Vec<String>, source: Source) -> Self {
        Prefetcher { cache, licenses: licenses.iter().map(|l| l.to_lowercase()).collect(), source,
                     status: Arc::new(Mutex::new(None)), active: None }
    }

    pub fn status(&self) -> Option<Status> {
        self.status.lock().ok().and_then(|s| s.clone())
    }

    /// Start work for a new manifest; repeated offers of the same deployment are ignored.
    pub fn offer(&mut self, manifest: &serde_json::Value) -> Option<String> {
        let dep = manifest["deployment_id"].as_str()?.to_string();
        if self.active.as_deref() == Some(dep.as_str()) {
            return None;
        }
        self.active = Some(dep.clone());
        let fail = |msg: String| Status { deployment_id: dep.clone(), state: "failed", bytes_done: 0, bytes_total: 0,
                                          error: Some(msg) };
        let license = manifest["license"].as_str().unwrap_or("unknown").to_lowercase();
        if !self.licenses.contains(&license) {
            let msg = format!("model license {license} not accepted by the device owner (--accept-license)");
            *self.status.lock().unwrap() = Some(fail(msg.clone()));
            return Some(format!("prefetch refused: {msg}"));
        }
        let items = match parse_items(manifest) {
            Ok(i) => i,
            Err(e) => {
                *self.status.lock().unwrap() = Some(fail(e.clone()));
                return Some(format!("prefetch refused: {e}"));
            }
        };
        let url = manifest["url"].as_str().unwrap_or_default().to_string();
        let total: u64 = items.iter().map(|i| i.nbytes).sum();
        let count = items.len();
        *self.status.lock().unwrap() = Some(Status { deployment_id: dep.clone(), state: "fetching", bytes_done: 0,
                                                     bytes_total: total, error: None });
        let (status, cache, source) = (self.status.clone(), self.cache.clone(), self.source.clone());
        std::thread::spawn(move || {
            let result = fetch_all(&cache, &source, &url, &items, &status);
            let mut s = status.lock().unwrap();
            if let Some(st) = s.as_mut().filter(|st| st.deployment_id == dep) {
                match result {
                    Ok(()) => st.state = "ready",
                    Err(e) => {
                        st.state = "failed";
                        st.error = Some(e.chars().take(280).collect());
                    }
                }
                println!("prefetch {}: {} ({} of {} bytes)", st.state, dep, st.bytes_done, st.bytes_total);
            }
        });
        Some(format!("prefetch started: {count} tensors, {:.1} MB", total as f64 / 1e6))
    }
}

fn index_path(cache: &Path) -> PathBuf {
    cache.join("difa-index.json")
}

fn load_index(cache: &Path) -> HashMap<String, String> {
    std::fs::read(index_path(cache)).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

fn fetch_all(cache: &Path, source: &Source, url: &str, items: &[Item], status: &Arc<Mutex<Option<Status>>>)
             -> Result<(), String> {
    let rpc = cache.join("rpc");
    std::fs::create_dir_all(&rpc).map_err(|e| format!("cannot create cache directory: {e}"))?;
    let mut index = load_index(cache);
    let (connect, total) = (Duration::from_secs(15), Duration::from_secs(6 * 3600));
    let (full_url, auth) = if url.starts_with('/') {
        (format!("{}{}", source.coordinator.trim_end_matches('/'), url), true)
    } else {
        (url.to_string(), false)
    };
    // The device token goes only to the pinned coordinator; a model origin gets public-CA HTTPS, no token.
    let http = if auth {
        crate::control::client(&source.coordinator, source.fingerprint.as_deref(), connect, total)?.0
    } else {
        reqwest::blocking::Client::builder().connect_timeout(connect).timeout(total).build()
            .map_err(|e| e.to_string())?
    };
    let add = |n: i64| {
        if let Some(s) = status.lock().unwrap().as_mut() {
            s.bytes_done = s.bytes_done.saturating_add_signed(n);
        }
    };
    for item in items {
        // Already verified on an earlier run (same content hash, file present with the right size).
        if let Some(name) = index.get(&item.sha256) {
            if std::fs::metadata(rpc.join(name)).is_ok_and(|m| m.len() == item.nbytes) {
                add(item.nbytes as i64);
                continue;
            }
        }
        let mut last = String::new();
        let mut done = false;
        for _attempt in 0..3 {
            match fetch_one(&http, &full_url, auth, source, item, &rpc, &add) {
                Ok(name) => {
                    index.insert(item.sha256.clone(), name);
                    let _ = std::fs::write(index_path(cache), serde_json::to_vec(&index).unwrap_or_default());
                    done = true;
                    break;
                }
                Err((e, fatal)) => {
                    last = e;
                    if fatal {
                        break;
                    }
                }
            }
        }
        if !done {
            return Err(format!("{}: {last}", item.name));
        }
    }
    Ok(())
}

/// Download, hash, verify, then publish under the FNV name. Err((message, fatal)).
fn fetch_one(http: &reqwest::blocking::Client, url: &str, auth: bool, source: &Source, item: &Item, rpc: &Path,
             add: &dyn Fn(i64)) -> Result<String, (String, bool)> {
    let mut req = http.get(url).header("Range", format!("bytes={}-{}", item.offset, item.offset + item.nbytes - 1));
    if auth {
        req = req.bearer_auth(&source.device_token);
    }
    let mut resp = req.send().map_err(|e| (format!("download failed: {e}"), false))?;
    if resp.status().as_u16() != 206 {
        return Err((format!("source did not return the requested range ({})", resp.status()), true));
    }
    let part = rpc.join(format!(".{}.part", item.sha256));
    let mut out = std::fs::File::create(&part).map_err(|e| (e.to_string(), true))?;
    let (mut sha, mut fnv, mut got) = (Sha256::new(), Fnv::new(), 0u64);
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = resp.read(&mut buf).map_err(|e| (format!("download interrupted: {e}"), false))?;
        if n == 0 {
            break;
        }
        got += n as u64;
        if got > item.nbytes {
            let _ = std::fs::remove_file(&part);
            add(-(got as i64 - n as i64));
            return Err(("source sent more bytes than requested".into(), true));
        }
        sha.update(&buf[..n]);
        fnv.update(&buf[..n]);
        out.write_all(&buf[..n]).map_err(|e| (format!("cannot write cache: {e}"), true))?;
        add(n as i64);
    }
    drop(out);
    let digest: String = sha.finalize().iter().map(|b| format!("{b:02x}")).collect();
    if got != item.nbytes || digest != item.sha256 {
        let _ = std::fs::remove_file(&part);
        add(-(got as i64)); // roll back progress for the failed attempt
        return Err((format!("verification failed ({got} bytes, sha256 {digest})"), false));
    }
    let name = fnv.name();
    std::fs::rename(&part, rpc.join(&name)).map_err(|e| (e.to_string(), true))?;
    Ok(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnv_matches_ggml_rpc() {
        let mut f = Fnv::new();
        f.update(b"");
        assert_eq!(f.name(), "cbf29ce484222325");
        let mut f = Fnv::new();
        f.update(b"a");
        assert_eq!(f.name(), "af63dc4c8601ec8c"); // published FNV-1a 64 test vector
    }

    #[test]
    fn manifest_parsing_requires_hashes() {
        let ok = serde_json::json!({"tensors": [{"name": "t", "offset": 1, "nbytes": 2, "sha256": "a".repeat(64)}]});
        assert_eq!(parse_items(&ok).unwrap().len(), 1);
        let bad = serde_json::json!({"tensors": [{"name": "t", "offset": 1, "nbytes": 2}]});
        assert!(parse_items(&bad).is_err());
    }

    #[test]
    fn unaccepted_license_fails_without_downloading() {
        let dir = std::env::temp_dir().join(format!("difa-prefetch-test-{}", std::process::id()));
        let mut p = Prefetcher::new(dir.clone(), vec!["mit".into()],
                                    Source { coordinator: "http://127.0.0.1:1".into(), device_token: "t".into(),
                                                     fingerprint: None });
        let m = serde_json::json!({"deployment_id": "dep_1", "license": "apache-2.0", "url": "/x", "tensors": []});
        assert!(p.offer(&m).unwrap().contains("not accepted"));
        let s = p.status().unwrap();
        assert_eq!((s.state, s.deployment_id.as_str()), ("failed", "dep_1"));
        assert!(p.offer(&m).is_none()); // same deployment is not retried in a loop
        assert!(!dir.exists());
    }
}
