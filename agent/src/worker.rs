//! Agent-managed inference worker (llama.cpp rpc-server) under the owner's memory cap (FR 07 to FR 09).
//!
//! llama.cpp's RPC worker advertises all host memory and has no cap option (Phase 1 finding), so the
//! agent owns the process: it starts the worker only while contribution is allowed and the lease is
//! valid, stops it on pause or lease expiry, and kills it if resident memory exceeds the cap. The
//! watchdog polls every 250 ms, so memory can exceed the cap for at most one poll interval; hard
//! prevention needs OS limits (cgroups on Linux, Job Objects on Windows), which are a Phase 2 item.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use sysinfo::{Pid, ProcessesToUpdate, System};
use tracing::{debug, info, warn};

/// Lines of worker output kept in memory, reported when the worker exits unexpectedly.
const TAIL_LINES: usize = 40;

/// What a watchdog tick changed, so the caller can react (for example report a cap kill at once).
#[derive(Debug, PartialEq)]
pub enum Change {
    None,
    Started,
    Stopped,
    Killed,
    Failed,
}

#[derive(Clone, Debug)]
pub struct WorkerConfig {
    pub bin: PathBuf,
    pub host: String,
    pub port: u16,
    pub threads: u32,
    /// Address the coordinator dials: the mTLS tunnel when one runs, otherwise the worker itself.
    pub advertise: String,
    /// "mtls" when the worker is reachable only through the tunnel, "plaintext" otherwise.
    pub transport: &'static str,
    /// Directory for llama.cpp's weight cache (LLAMA_CACHE); the worker runs with -c when set.
    pub cache_dir: Option<PathBuf>,
}

impl WorkerConfig {
    pub fn endpoint(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
}

#[derive(Debug, PartialEq)]
pub enum Decision {
    Start,
    Stop(&'static str),
    Kill(&'static str),
    Keep,
}

/// Pure decision rule so the policy can be tested without processes.
pub fn decide(should_run: bool, running: bool, rss: Option<u64>, cap: u64) -> Decision {
    if running {
        if rss.is_some_and(|r| r > cap) {
            return Decision::Kill("memory cap exceeded");
        }
        if !should_run {
            return Decision::Stop("contribution paused or lease not valid");
        }
        return Decision::Keep;
    }
    if should_run {
        Decision::Start
    } else {
        Decision::Keep
    }
}

pub struct Worker {
    cfg: WorkerConfig,
    child: Option<Child>,
    sys: System,
    pub rss: u64,
    pub peak_rss: u64,
    pub cap_kills: u32,
    /// Times the worker exited on its own (crashed); each is followed by a restart when contribution allows.
    pub restarts: u32,
    pub last_stop: Option<String>,
    /// After a cap kill, stay stopped until the owner raises the cap above what the worker needed.
    blocked_below: Option<u64>,
    /// The most recent worker output lines (stdout and stderr), for exit reports.
    tail: Arc<Mutex<VecDeque<String>>>,
    /// Set while the agent itself is stopping the worker, so that exit is not reported as a crash.
    stopping: bool,
}

impl Worker {
    pub fn new(cfg: WorkerConfig) -> Self {
        Worker { cfg, child: None, sys: System::new(), rss: 0, peak_rss: 0, cap_kills: 0, restarts: 0, last_stop: None,
                 blocked_below: None, tail: Arc::new(Mutex::new(VecDeque::new())), stopping: false }
    }

    pub fn running(&mut self) -> bool {
        match self.child.as_mut().map(|c| c.try_wait()) {
            Some(Ok(None)) => true,
            Some(result) => {
                let status = match result { Ok(Some(s)) => s.to_string(), Ok(None) => "unknown".into(), Err(e) => e.to_string() };
                self.child = None;
                if !self.stopping {
                    self.restarts += 1;
                    // Give the output threads a moment to collect the last lines before reporting.
                    std::thread::sleep(std::time::Duration::from_millis(100));
                    let tail: Vec<String> = self.tail.lock().map(|t| t.iter().cloned().collect()).unwrap_or_default();
                    warn!(error_code = "WORKER_EXITED", exit = %status, last_output = %tail.join("\n"),
                          "worker exited on its own ({status})");
                }
                self.last_stop.get_or_insert_with(|| "worker exited".into());
                false
            }
            None => false,
        }
    }

    fn measure(&mut self) -> Option<u64> {
        let pid = Pid::from_u32(self.child.as_ref()?.id());
        self.sys.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);
        let rss = self.sys.process(pid)?.memory();
        self.rss = rss;
        self.peak_rss = self.peak_rss.max(rss);
        Some(rss)
    }

    /// Apply the decision rule once, logging anything that changed.
    pub fn tick(&mut self, should_run: bool, cap: u64) -> Change {
        let running = self.running();
        let rss = if running { self.measure() } else { None };
        let allowed = should_run && self.blocked_below.is_none_or(|need| cap > need);
        match decide(allowed, running, rss, cap) {
            Decision::Start => match self.command().spawn() {
                Ok(mut child) => {
                    self.capture(&mut child);
                    self.child = Some(child);
                    self.blocked_below = None;
                    self.last_stop = None;
                    self.stopping = false;
                    info!("worker started on {}", self.cfg.endpoint());
                    Change::Started
                }
                Err(e) => {
                    warn!(error_code = "WORKER_START_FAILED", bin = %self.cfg.bin.display(),
                          "worker failed to start: {e}");
                    Change::Failed
                }
            },
            Decision::Stop(reason) => {
                self.halt(reason);
                info!("worker stopped: {reason}");
                Change::Stopped
            }
            Decision::Kill(reason) => {
                let used = self.rss;
                self.halt(reason);
                self.cap_kills += 1;
                self.blocked_below = Some(used);
                warn!(error_code = "MEMORY_CAP_EXCEEDED", used_bytes = used, cap_bytes = cap,
                      "worker killed: {reason} ({:.0} MB used, cap {:.0} MB)", used as f64 / 1e6, cap as f64 / 1e6);
                Change::Killed
            }
            Decision::Keep => Change::None,
        }
    }

    /// Forward the worker's output to the log (tagged with the node, deployment and requests) and keep
    /// the last lines for exit reports. rpc-server handles tensors only, never prompt text.
    fn capture(&self, child: &mut Child) {
        fn pump(stream: impl Read + Send + 'static, tail: Arc<Mutex<VecDeque<String>>>) {
            std::thread::spawn(move || {
                for line in BufReader::new(stream).lines().map_while(Result::ok) {
                    let line: String = line.chars().take(500).collect();
                    debug!(target: "idlemesh_agent::worker_output", source = "rpc-server", "{line}");
                    if let Ok(mut t) = tail.lock() {
                        if t.len() == TAIL_LINES {
                            t.pop_front();
                        }
                        t.push_back(line);
                    }
                }
            });
        }
        if let Ok(mut t) = self.tail.lock() {
            t.clear();
        }
        if let Some(out) = child.stdout.take() {
            pump(out, self.tail.clone());
        }
        if let Some(err) = child.stderr.take() {
            pump(err, self.tail.clone());
        }
    }

    fn command(&self) -> Command {
        let mut cmd = Command::new(&self.cfg.bin);
        cmd.args(["-H", &self.cfg.host, "-p", &self.cfg.port.to_string(), "-d", "CPU", "-t",
                  &self.cfg.threads.to_string()]);
        if let Some(dir) = &self.cfg.cache_dir {
            cmd.arg("-c").env("LLAMA_CACHE", dir);
        }
        cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
        cmd
    }

    pub fn halt(&mut self, reason: &str) {
        self.stopping = true;
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        self.rss = 0;
        self.last_stop = Some(reason.to_string());
    }

    pub fn status(&mut self) -> serde_json::Value {
        let running = self.running();
        serde_json::json!({
            "state": if running { "running" } else { "stopped" },
            "endpoint": self.cfg.advertise,
            "transport": self.cfg.transport,
            "rss_bytes": if running { self.rss } else { 0 },
            "peak_rss_bytes": self.peak_rss,
            "cap_kills": self.cap_kills,
            "last_stop_reason": self.last_stop,
        })
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        self.halt("agent exiting");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn starts_only_when_allowed() {
        assert_eq!(decide(true, false, None, 100), Decision::Start);
        assert_eq!(decide(false, false, None, 100), Decision::Keep);
    }

    #[test]
    fn stops_when_paused_or_lease_invalid() {
        assert!(matches!(decide(false, true, Some(10), 100), Decision::Stop(_)));
    }

    #[test]
    fn kills_above_cap_even_when_allowed() {
        assert!(matches!(decide(true, true, Some(101), 100), Decision::Kill(_)));
        assert_eq!(decide(true, true, Some(100), 100), Decision::Keep);
    }

    #[test]
    fn cap_kill_takes_priority_over_stop() {
        assert!(matches!(decide(false, true, Some(500), 100), Decision::Kill(_)));
    }
}
