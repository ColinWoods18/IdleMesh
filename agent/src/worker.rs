//! Agent-managed inference worker (llama.cpp rpc-server) under the owner's memory cap (FR 07 to FR 09).
//!
//! llama.cpp's RPC worker advertises all host memory and has no cap option (Phase 1 finding), so the
//! agent owns the process: it starts the worker only while contribution is allowed and the lease is
//! valid, stops it on pause or lease expiry, and kills it if resident memory exceeds the cap. The
//! watchdog polls every 250 ms, so memory could exceed the cap for one poll interval; an OS limit set at start
//! (S4.8) closes that gap for the memory the worker allocates itself:
//! - Windows: a Job Object with a per-process memory limit (committed memory), which also kills the worker if the
//!   agent dies, so no worker outlives its supervisor;
//! - Linux: RLIMIT_DATA (heap and private mappings; the memory-mapped model file is not counted, as the OS can
//!   drop those pages);
//! - macOS: no per-process limit is enforced by the OS, so the watchdog alone applies.
//!
//! As a service (docs/design/agent-service.md) Linux nodes add a cgroup MemoryMax on the unit.

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

/// The OS-level memory limit on a running worker; dropping it lifts the limit (and on Windows ends the worker).
pub struct OsLimit {
    #[cfg(windows)]
    _job: win_job::Job,
}

/// Start `cmd` under a hard OS memory limit of `cap_bytes` where the platform has one. Returns the child and
/// whether a limit applies (None: no OS limit on this platform, or it failed; the watchdog still enforces the cap).
pub fn spawn_limited(mut cmd: Command, cap_bytes: u64) -> std::io::Result<(Child, Option<OsLimit>)> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        let lim = libc::rlimit { rlim_cur: cap_bytes as libc::rlim_t, rlim_max: cap_bytes as libc::rlim_t };
        // SAFETY: setrlimit is async-signal-safe and only touches the child between fork and exec.
        unsafe {
            cmd.pre_exec(move || {
                if libc::setrlimit(libc::RLIMIT_DATA, &lim) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let child = cmd.spawn()?;
        Ok((child, Some(OsLimit {})))
    }
    #[cfg(windows)]
    {
        let child = cmd.spawn()?;
        match win_job::apply(&child, cap_bytes) {
            Ok(job) => Ok((child, Some(OsLimit { _job: job }))),
            Err(e) => {
                warn!(error_code = "OS_MEMORY_LIMIT_FAILED", "cannot apply the OS memory limit to the worker: {e}");
                Ok((child, None))
            }
        }
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = cap_bytes;
        Ok((cmd.spawn()?, None))
    }
}

#[cfg(windows)]
mod win_job {
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    };

    pub struct Job(HANDLE);

    // SAFETY: a job handle may be used and closed from any thread.
    unsafe impl Send for Job {}

    impl Drop for Job {
        fn drop(&mut self) {
            // SAFETY: the handle came from CreateJobObjectW and is closed once. KILL_ON_JOB_CLOSE ends the worker.
            unsafe { CloseHandle(self.0) };
        }
    }

    pub fn apply(child: &std::process::Child, cap_bytes: u64) -> Result<Job, String> {
        use std::os::windows::io::AsRawHandle;
        let err = || std::io::Error::last_os_error().to_string();
        // SAFETY: plain Win32 calls with valid arguments; the job handle is owned by `Job` from here on.
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err(err());
            }
            let job = Job(handle);
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            info.ProcessMemoryLimit = cap_bytes as usize;
            let ptr: *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION = &info;
            if SetInformationJobObject(job.0, JobObjectExtendedLimitInformation, ptr.cast(),
                                       std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32) == 0 {
                return Err(err());
            }
            if AssignProcessToJobObject(job.0, child.as_raw_handle() as HANDLE) == 0 {
                return Err(err());
            }
            Ok(job)
        }
    }
}

pub struct Worker {
    cfg: WorkerConfig,
    child: Option<Child>,
    os_limit: Option<OsLimit>,
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
    /// The last unexpected exit (status, last output lines), until the agent saves a crash report for it.
    crash: Option<(String, Vec<String>)>,
    /// CPU priority currently set on the running worker (S4.9).
    priority: Option<Priority>,
    /// After a crash, no restart before this time (doubling backoff, so a worker that dies at start does not
    /// restart four times a second and flood the node with crash reports).
    restart_after: Option<std::time::Instant>,
    backoff: std::time::Duration,
    started_at: Option<std::time::Instant>,
}

const BACKOFF_MIN: std::time::Duration = std::time::Duration::from_secs(1);
const BACKOFF_MAX: std::time::Duration = std::time::Duration::from_secs(60);
/// A worker that ran this long before crashing starts its backoff again from the minimum.
const STABLE_RUN: std::time::Duration = std::time::Duration::from_secs(60);

/// Worker CPU priority by contribution level (S4.9): lowest while yielding to a user, low while ramping, normal
/// when the machine is fully available.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Priority {
    Idle,
    BelowNormal,
    Normal,
}

impl Priority {
    pub fn for_level(level: f64) -> Self {
        if level < 0.25 {
            Priority::Idle
        } else if level < 0.75 {
            Priority::BelowNormal
        } else {
            Priority::Normal
        }
    }
}

#[cfg(windows)]
fn set_priority(pid: u32, _current: Option<Priority>, want: Priority) -> Result<Priority, String> {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, SetPriorityClass, BELOW_NORMAL_PRIORITY_CLASS, IDLE_PRIORITY_CLASS, NORMAL_PRIORITY_CLASS,
        PROCESS_SET_INFORMATION,
    };
    let class = match want {
        Priority::Idle => IDLE_PRIORITY_CLASS,
        Priority::BelowNormal => BELOW_NORMAL_PRIORITY_CLASS,
        Priority::Normal => NORMAL_PRIORITY_CLASS,
    };
    // SAFETY: the handle is opened for this call only and closed below.
    unsafe {
        let h = OpenProcess(PROCESS_SET_INFORMATION, 0, pid);
        if h.is_null() {
            return Err(std::io::Error::last_os_error().to_string());
        }
        let ok = SetPriorityClass(h, class);
        CloseHandle(h);
        if ok == 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
    }
    Ok(want)
}

/// Unix: a nice value an ordinary user can raise but not lower again, so the priority only goes down while the
/// worker runs; it starts again from the level in force at its next start.
#[cfg(unix)]
fn set_priority(pid: u32, current: Option<Priority>, want: Priority) -> Result<Priority, String> {
    let nice = |p: Priority| match p {
        Priority::Idle => 19,
        Priority::BelowNormal => 10,
        Priority::Normal => 0,
    };
    if current.is_some_and(|c| nice(c) >= nice(want)) {
        return Ok(current.unwrap_or(want)); // cannot raise priority back without privileges
    }
    // SAFETY: setpriority on our own child process.
    if unsafe { libc::setpriority(libc::PRIO_PROCESS, pid as libc::id_t, nice(want)) } != 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    Ok(want)
}

impl Worker {
    pub fn new(cfg: WorkerConfig) -> Self {
        Worker { cfg, child: None, os_limit: None, sys: System::new(), rss: 0, peak_rss: 0, cap_kills: 0, restarts: 0, last_stop: None,
                 blocked_below: None, tail: Arc::new(Mutex::new(VecDeque::new())), stopping: false, crash: None,
                 priority: None, restart_after: None, backoff: BACKOFF_MIN, started_at: None }
    }

    /// The worker's last unexpected exit, once.
    pub fn take_crash(&mut self) -> Option<(String, Vec<String>)> {
        self.crash.take()
    }

    /// Threads the worker starts with next time (S4.9: from the governor's ceiling).
    pub fn set_threads(&mut self, threads: u32) {
        self.cfg.threads = threads.max(1);
    }

    pub fn threads(&self) -> u32 {
        self.cfg.threads
    }

    /// Apply a contribution level to the running worker's CPU priority (S4.9). Returns the priority now in force.
    pub fn apply_level(&mut self, level: f64) -> Option<Priority> {
        let want = Priority::for_level(level);
        let child = self.child.as_ref()?;
        if self.priority == Some(want) {
            return self.priority;
        }
        match set_priority(child.id(), self.priority, want) {
            Ok(p) => {
                if self.priority != Some(p) {
                    debug!(priority = ?p, level, "worker priority set to {p:?}");
                }
                self.priority = Some(p);
            }
            Err(e) => debug!("cannot change the worker's priority: {e}"),
        }
        self.priority
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
                    self.crash = Some((status, tail)); // the agent turns this into a crash report (S4.3)
                    let now = std::time::Instant::now();
                    if self.started_at.is_some_and(|t| now.duration_since(t) >= STABLE_RUN) {
                        self.backoff = BACKOFF_MIN;
                    }
                    self.restart_after = Some(now + self.backoff);
                    info!("worker restarts in {} s at the earliest", self.backoff.as_secs());
                    self.backoff = (self.backoff * 2).min(BACKOFF_MAX);
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
        let allowed = should_run && self.blocked_below.is_none_or(|need| cap > need)
            && self.restart_after.is_none_or(|t| std::time::Instant::now() >= t);
        match decide(allowed, running, rss, cap) {
            Decision::Start => match spawn_limited(self.command(), cap) {
                Ok((mut child, limit)) => {
                    self.capture(&mut child);
                    self.child = Some(child);
                    if limit.is_none() && cfg!(target_os = "macos") {
                        debug!("macOS has no per-process memory limit: the watchdog alone enforces the cap");
                    }
                    self.os_limit = limit;
                    self.started_at = Some(std::time::Instant::now());
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
        self.os_limit = None;
        self.priority = None;
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
            "last_stop_reason": self.last_stop.as_deref().map(|s| s.chars().take(200).collect::<String>()),
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
