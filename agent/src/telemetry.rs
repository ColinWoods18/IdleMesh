//! Telemetry buffer (S2.4): records wait on disk until the coordinator has accepted them, so an outage or a
//! sleeping laptop loses nothing within the size limit.
//!
//! Records are appended as JSON lines to `current.jsonl`. Before an upload the file is sealed into a
//! numbered segment, and segments are sent oldest first and deleted only after the coordinator accepts
//! them. When the buffer would exceed its limit, the oldest segments are deleted and their records counted
//! as dropped; the count travels with the next batch so the loss is visible on the coordinator. A crash
//! can leave a half-written last line, which is skipped when read.

use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use tracing::{error, warn};

pub const SCHEMA_VERSION: u32 = 1;
const CURRENT: &str = "current.jsonl";
const DROPPED: &str = "dropped";
/// A segment is sealed once it reaches this size even without an upload attempt.
const SEGMENT_BYTES: u64 = 64 * 1024;
/// Records per upload; well under the coordinator's 1 MiB body limit.
pub const BATCH_RECORDS: usize = 1000;
/// Batches per flush, so a long backfill does not hold up the agent's main loop.
const BATCHES_PER_FLUSH: usize = 5;

pub struct Buffer {
    dir: PathBuf,
    max_bytes: u64,
    dropped: u64,
    next_seq: u64,
}

pub struct Batch {
    pub records: Vec<Value>,
    pub dropped: u64,
    files: Vec<PathBuf>,
}

impl Batch {
    pub fn body(&self) -> Value {
        json!({"schema_version": SCHEMA_VERSION, "records": self.records, "dropped_since_last": self.dropped})
    }
}

pub enum SendError {
    /// Not delivered (network, timeout, coordinator error, no consent yet): keep the batch and retry later.
    Retry(String),
    /// The coordinator says the batch itself is invalid: it would be refused forever, so it is dropped.
    Invalid(String),
}

impl Buffer {
    pub fn open(dir: &Path, max_bytes: u64) -> io::Result<Self> {
        fs::create_dir_all(dir)?;
        let dropped = fs::read_to_string(dir.join(DROPPED)).ok().and_then(|s| s.trim().parse().ok()).unwrap_or(0);
        let mut b = Buffer { dir: dir.to_path_buf(), max_bytes: max_bytes.max(SEGMENT_BYTES), dropped, next_seq: 0 };
        b.next_seq = b.segments()?.last().and_then(|p| seq_of(p)).map_or(0, |s| s + 1);
        Ok(b)
    }

    pub fn push(&mut self, record: &Value) -> io::Result<()> {
        let line = format!("{record}\n");
        let current = self.dir.join(CURRENT);
        let size = fs::metadata(&current).map_or(0, |m| m.len());
        if size > 0 && size + line.len() as u64 > SEGMENT_BYTES {
            self.seal()?;
        }
        OpenOptions::new().create(true).append(true).open(&current)?.write_all(line.as_bytes())?;
        self.enforce_cap()
    }

    /// Bytes on disk, for health reporting.
    pub fn bytes(&self) -> u64 {
        fs::read_dir(&self.dir).map_or(0, |rd| rd.flatten().filter_map(|e| e.metadata().ok()).map(|m| m.len()).sum())
    }

    /// Nothing waiting to be sent.
    pub fn is_empty(&self) -> bool {
        self.segments().is_ok_and(|s| s.is_empty()) && fs::metadata(self.dir.join(CURRENT)).map_or(0, |m| m.len()) == 0
    }

    /// Records dropped since the coordinator last heard about drops.
    pub fn dropped(&self) -> u64 {
        self.dropped
    }

    /// The oldest whole segments, up to `max_records` records (at least one segment).
    pub fn next_batch(&mut self, max_records: usize) -> io::Result<Option<Batch>> {
        if self.segments()?.is_empty() {
            self.seal()?;
        }
        let mut batch = Batch { records: Vec::new(), dropped: self.dropped, files: Vec::new() };
        for seg in self.segments()? {
            let records = read_records(&seg)?;
            if !batch.files.is_empty() && batch.records.len() + records.len() > max_records {
                break;
            }
            batch.records.extend(records);
            batch.files.push(seg);
        }
        Ok((!batch.files.is_empty()).then_some(batch))
    }

    /// The coordinator accepted the batch (or refused it for good): delete its segments.
    pub fn commit(&mut self, batch: &Batch) -> io::Result<()> {
        for f in &batch.files {
            fs::remove_file(f)?;
        }
        self.dropped = self.dropped.saturating_sub(batch.dropped);
        self.save_dropped()
    }

    /// Send batches until the buffer is empty, a send fails, or the per-flush limit is reached.
    /// Returns the number of records delivered.
    pub fn flush(&mut self, mut send: impl FnMut(&Value) -> Result<(), SendError>) -> usize {
        let mut sent = 0;
        for _ in 0..BATCHES_PER_FLUSH {
            let batch = match self.next_batch(BATCH_RECORDS) {
                Ok(Some(b)) => b,
                Ok(None) => break,
                Err(e) => {
                    error!(error_code = "TELEMETRY_BUFFER_IO", "cannot read the telemetry buffer: {e}");
                    break;
                }
            };
            let mut refused = 0;
            match send(&batch.body()) {
                Ok(()) => sent += batch.records.len(),
                Err(SendError::Retry(why)) => {
                    warn!(error_code = "TELEMETRY_UPLOAD_FAILED", buffered = batch.records.len(),
                          "telemetry upload failed; keeping it on disk: {why}");
                    break;
                }
                Err(SendError::Invalid(why)) => {
                    error!(error_code = "TELEMETRY_REJECTED", records = batch.records.len(),
                           "the coordinator refused a telemetry batch; dropping it: {why}");
                    refused = batch.records.len() as u64;
                }
            }
            let done = self.commit(&batch).and_then(|()| {
                self.dropped += refused; // counted, so the coordinator hears about the loss with the next batch
                self.save_dropped()
            });
            if let Err(e) = done {
                error!(error_code = "TELEMETRY_BUFFER_IO", "cannot remove sent telemetry: {e}");
                break;
            }
        }
        sent
    }

    fn seal(&mut self) -> io::Result<()> {
        let current = self.dir.join(CURRENT);
        if fs::metadata(&current).map_or(0, |m| m.len()) == 0 {
            return Ok(());
        }
        let name = format!("seg-{:012}.jsonl", self.next_seq);
        self.next_seq += 1;
        fs::rename(&current, self.dir.join(name))
    }

    fn segments(&self) -> io::Result<Vec<PathBuf>> {
        let mut v: Vec<PathBuf> = fs::read_dir(&self.dir)?.flatten().map(|e| e.path())
            .filter(|p| seq_of(p).is_some()).collect();
        v.sort();
        Ok(v)
    }

    /// Delete the oldest records until the buffer fits its limit, counting what was lost.
    fn enforce_cap(&mut self) -> io::Result<()> {
        let mut lost = 0u64;
        while self.bytes() > self.max_bytes {
            let oldest = match self.segments()?.first() {
                Some(p) => p.clone(),
                None => {
                    self.seal()?; // only the current file is left: it goes too
                    match self.segments()?.first() {
                        Some(p) => p.clone(),
                        None => break,
                    }
                }
            };
            lost += read_records(&oldest)?.len() as u64;
            fs::remove_file(&oldest)?;
        }
        if lost > 0 {
            self.dropped += lost;
            self.save_dropped()?;
            warn!(error_code = "TELEMETRY_BUFFER_FULL", dropped = lost,
                  "telemetry buffer full: dropped the {lost} oldest records");
        }
        Ok(())
    }

    fn save_dropped(&self) -> io::Result<()> {
        fs::write(self.dir.join(DROPPED), self.dropped.to_string())
    }
}

fn seq_of(p: &Path) -> Option<u64> {
    p.file_name()?.to_str()?.strip_prefix("seg-")?.strip_suffix(".jsonl")?.parse().ok()
}

fn read_records(p: &Path) -> io::Result<Vec<Value>> {
    Ok(fs::read_to_string(p)?.lines().filter_map(|l| serde_json::from_str(l).ok()).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("idlemesh-telemetry-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        d
    }

    fn rec(i: usize) -> Value {
        json!({"kind": "idle", "n": i, "pad": "x".repeat(200)})
    }

    #[test]
    fn records_survive_until_accepted_and_arrive_in_order() {
        let dir = temp("order");
        let mut b = Buffer::open(&dir, 10_000_000).unwrap();
        for i in 0..2500 {
            b.push(&rec(i)).unwrap();
        }
        // The coordinator is down: nothing is lost.
        assert_eq!(b.flush(|_| Err(SendError::Retry("offline".into()))), 0);
        // A restart reopens the same buffer.
        drop(b);
        let mut b = Buffer::open(&dir, 10_000_000).unwrap();
        let mut got = Vec::new();
        while b.flush(|body| {
            got.extend(body["records"].as_array().unwrap().iter().map(|r| r["n"].as_u64().unwrap()));
            Ok(())
        }) > 0 {}
        assert_eq!(got, (0..2500).collect::<Vec<u64>>());
        assert!(b.next_batch(BATCH_RECORDS).unwrap().is_none());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_full_buffer_drops_the_oldest_and_reports_the_count() {
        let dir = temp("cap");
        let mut b = Buffer::open(&dir, 200_000).unwrap();
        for i in 0..3000 {
            b.push(&rec(i)).unwrap();
        }
        assert!(b.bytes() <= 200_000, "{}", b.bytes());
        let dropped = b.dropped();
        assert!(dropped > 0);
        let mut first = None;
        let mut reported = 0;
        let mut kept = 0;
        while b.flush(|body| {
            first.get_or_insert(body["records"][0]["n"].as_u64().unwrap());
            reported += body["dropped_since_last"].as_u64().unwrap();
            kept += body["records"].as_array().unwrap().len() as u64;
            Ok(())
        }) > 0 {}
        assert_eq!(first, Some(dropped)); // the oldest went first
        assert_eq!(reported, dropped); // reported once, then cleared
        assert_eq!(kept + dropped, 3000);
        assert_eq!(b.dropped(), 0);
        assert_eq!(Buffer::open(&dir, 200_000).unwrap().dropped(), 0);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_torn_last_line_is_skipped() {
        let dir = temp("torn");
        let mut b = Buffer::open(&dir, 1_000_000).unwrap();
        b.push(&rec(1)).unwrap();
        OpenOptions::new().append(true).open(dir.join(CURRENT)).unwrap().write_all(b"{\"kind\":\"id").unwrap();
        let batch = b.next_batch(BATCH_RECORDS).unwrap().unwrap();
        assert_eq!(batch.records.len(), 1);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn an_invalid_batch_is_dropped_not_retried_forever() {
        let dir = temp("invalid");
        let mut b = Buffer::open(&dir, 1_000_000).unwrap();
        b.push(&rec(1)).unwrap();
        b.flush(|_| Err(SendError::Invalid("422".into())));
        assert!(b.next_batch(BATCH_RECORDS).unwrap().is_none());
        assert_eq!(b.dropped(), 1);
        fs::remove_dir_all(&dir).ok();
    }
}
