//! idlemesh-agent: enrolls a device with a one-time code, then heartbeats with probed capabilities and
//! the locally enforced contribution state. Control transport is HTTPS/JSON with a device token and
//! the coordinator's certificate pinned at enrollment (see control.rs); the spec's gRPC over mutual
//! TLS replaces it in Phase 2. The inference data path to a remote worker runs over mutual TLS (see
//! tunnel.rs).

mod control;
mod diagnostics;
mod governor;
mod hardware;
mod health;
mod idle;
mod logging;
mod node_config;
mod node_state;
mod policy;
mod prefetch;
mod probe;
mod secure_fs;
mod telemetry;
mod tunnel;
mod worker;

use clap::{Parser, Subcommand};
use policy::{allocatable, effective_paused, merge, Lease, OwnerPolicy};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};
use tracing::{debug, error, info, warn};
use worker::{Worker, WorkerConfig};

const DEFAULT_BACKEND_DIGEST: &str = "fake-runtime-0.1";
const GB: f64 = 1e9;

#[derive(Parser)]
#[command(name = "idlemesh-agent", version, about = "IdleMesh node agent")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
    /// Log level: error, warn, info, debug or trace.
    #[arg(long, global = true, env = "IDLEMESH_LOG", default_value = "info")]
    log_level: String,
    /// Directory for the JSON log file (default: a logs folder next to the state file).
    #[arg(long, global = true)]
    log_dir: Option<PathBuf>,
    /// Rotate the log file at this size, in MB.
    #[arg(long, global = true, default_value_t = 10)]
    log_max_mb: u64,
    /// Number of rotated log files to keep.
    #[arg(long, global = true, default_value_t = 5)]
    log_keep: usize,
}

#[derive(Subcommand)]
#[allow(clippy::large_enum_variant)] // parsed once at startup; boxing `Run` would only add noise
enum Cmd {
    /// Enroll this device using a one-time code from the coordinator.
    Enroll {
        /// The coordinator's address: https://ADDRESS:8765 (plain http only to this machine's loopback).
        #[arg(long)]
        coordinator: String,
        /// SHA-256 fingerprint of the coordinator's certificate, as shown with the enrollment code (at
        /// least the first 16 hex digits). The code is sent only to a server presenting that certificate.
        #[arg(long)]
        coordinator_fingerprint: Option<String>,
        #[arg(long)]
        code: String,
        #[arg(long)]
        name: String,
        /// Most memory this device may contribute, in GB. Required: owners choose explicitly (FR 07).
        #[arg(long)]
        memory_cap_gb: f64,
        #[arg(long, default_value_t = false)]
        allow_battery: bool,
        #[arg(long, default_value = "idlemesh-agent-state.json")]
        state: PathBuf,
        /// Use a reference-device stand-in instead of probing this machine.
        #[arg(long)]
        simulate: Option<String>,
        /// Inference backend build this device runs (must match the coordinator's deployment).
        #[arg(long, default_value = DEFAULT_BACKEND_DIGEST)]
        backend_digest: String,
        /// The owner consents to idle-data collection: input idle time, lock state, CPU use and node state.
        #[arg(long)]
        consent_idle_data: bool,
        /// The owner consents to diagnostics: logs and crash reports the coordinator can request (S4).
        #[arg(long)]
        consent_diagnostics: bool,
    },
    /// Show or change this device's consent record. Restart `run` for a change to take effect.
    Consent {
        #[arg(long, default_value = "idlemesh-agent-state.json")]
        state: PathBuf,
        /// Idle-data collection: true or false.
        #[arg(long)]
        idle_data: Option<bool>,
        /// Diagnostics: true or false.
        #[arg(long)]
        diagnostics: Option<bool>,
    },
    /// Print this machine's idle readings (seconds since input, lock state, CPU use, node state).
    Idle {
        /// Keep printing one line a second until stopped.
        #[arg(long)]
        watch: bool,
        #[arg(long, default_value_t = DEFAULT_IDLE_AFTER_S)]
        idle_after_s: u64,
    },
    /// Send heartbeats and enforce owner policy.
    Run {
        #[arg(long, default_value = "idlemesh-agent-state.json")]
        state: PathBuf,
        /// Node config file (S4.2): enrolls on first start from its values, then runs. Sets the state file path.
        #[arg(long)]
        config: Option<PathBuf>,
        /// Test hook: panic after this many heartbeats, to exercise crash reports (S4.3).
        #[arg(long, hide = true)]
        debug_panic_after_beats: Option<u64>,
        /// Stop after this many heartbeats (for tests).
        #[arg(long)]
        beats: Option<u64>,
        /// Run and supervise a llama.cpp rpc-server under the owner's memory cap.
        #[arg(long)]
        worker_bin: Option<PathBuf>,
        #[arg(long, default_value = "127.0.0.1")]
        worker_host: String,
        #[arg(long, default_value_t = 50052)]
        worker_port: u16,
        #[arg(long, default_value_t = 4)]
        worker_threads: u32,
        /// Time real decoding with llama-bench at startup instead of reporting a memory-copy figure.
        #[arg(long)]
        decode_probe_bench: Option<PathBuf>,
        #[arg(long)]
        decode_probe_model: Option<PathBuf>,
        /// Catalog name of the probe model, so the coordinator can convert tokens/s to bandwidth.
        #[arg(long, default_value = "qwen2.5-0.5b-q8")]
        decode_probe_catalog: String,
        /// Serve the worker to the coordinator over mutual TLS on this address (for example 0.0.0.0:50100).
        /// The worker itself then stays on loopback.
        #[arg(long)]
        tunnel_listen: Option<String>,
        /// Address the coordinator should dial for the tunnel (for example 192.168.1.20:50100).
        #[arg(long)]
        tunnel_advertise: Option<String>,
        /// Allow the unencrypted llama.cpp worker on a non-loopback address. Isolated lab networks only.
        #[arg(long, default_value_t = false)]
        insecure_plaintext_worker: bool,
        /// Where this device keeps prefetched weights (default: difa-cache next to the state file).
        #[arg(long)]
        cache_dir: Option<PathBuf>,
        /// Model licenses the owner accepts for download on this device (repeatable, e.g. apache-2.0).
        #[arg(long = "accept-license")]
        accept_licenses: Vec<String>,
        /// Seconds between idle samples (default: the coordinator's site setting).
        #[arg(long)]
        telemetry_interval_s: Option<u64>,
        /// Seconds without input before the machine counts as idle (default: the coordinator's site setting).
        #[arg(long)]
        idle_after_s: Option<u64>,
        /// Most disk space buffered telemetry may use while the coordinator is unreachable, in MB.
        #[arg(long, default_value_t = 20)]
        telemetry_buffer_mb: u64,
    },
    /// Run only the mTLS tunnel in front of an already running worker (benchmarks and on-device checks).
    Tunnel {
        #[arg(long)]
        listen: String,
        #[arg(long)]
        worker_port: u16,
        /// This device's certificate and key (PEM).
        #[arg(long)]
        cert: PathBuf,
        #[arg(long)]
        key: PathBuf,
        /// The only client certificate to accept (PEM): the coordinator's.
        #[arg(long)]
        peer_cert: PathBuf,
    },
    /// Print what this device would report, without contacting a coordinator.
    Probe {
        #[arg(long)]
        simulate: Option<String>,
    },
    /// Test helper: allocate and touch this many MB, then exit (0 when allocated, 3 when the OS refused).
    #[command(hide = true)]
    AllocTest {
        #[arg(long)]
        mb: u64,
    },
    /// Test helper (S4.8): run `alloc-test` under the worker's OS memory limit and report what happened.
    #[command(hide = true)]
    LimitTest {
        #[arg(long)]
        cap_mb: u64,
        #[arg(long)]
        alloc_mb: u64,
    },
}

/// Allocate `mb` megabytes and write to every page, so the memory is really committed.
fn alloc_test(mb: u64) -> ! {
    let bytes = (mb as usize) << 20;
    let mut v: Vec<u8> = Vec::new();
    if v.try_reserve_exact(bytes).is_err() {
        println!("refused");
        std::process::exit(3);
    }
    v.resize(bytes, 0);
    for i in (0..bytes).step_by(4096) {
        v[i] = 1;
    }
    println!("allocated {}", std::hint::black_box(&v).len() >> 20);
    std::process::exit(0);
}

fn limit_test(cap_mb: u64, alloc_mb: u64) -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let mut cmd = std::process::Command::new(exe);
    cmd.args(["alloc-test", "--mb", &alloc_mb.to_string()]).stdout(std::process::Stdio::piped());
    let (child, limit) = worker::spawn_limited(cmd, cap_mb << 20).map_err(|e| e.to_string())?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
    let outcome = if out.status.success() { "allocated" } else { "refused" };
    println!("{}", json!({"os_limit": limit.is_some(), "outcome": outcome, "exit": out.status.code(),
                          "child_said": stdout}));
    Ok(())
}

#[derive(Serialize, Deserialize)]
struct State {
    coordinator: String,
    device_id: String,
    device_token: String,
    name: String,
    simulate: Option<String>,
    owner_policy: OwnerPolicy,
    #[serde(default = "default_digest")]
    backend_digest: String,
    /// Device certificate and key for the worker tunnel, generated at enrollment (key never leaves here).
    #[serde(default)]
    tunnel_cert_pem: Option<String>,
    #[serde(default)]
    tunnel_key_pem: Option<String>,
    /// The coordinator's tunnel client certificate, pinned at enrollment.
    #[serde(default)]
    coordinator_cert_pem: Option<String>,
    /// Full SHA-256 of the coordinator's control (HTTPS) certificate, pinned at enrollment.
    #[serde(default)]
    coordinator_fingerprint: Option<String>,
    /// The owner's consent record (S2.6). Absent on devices enrolled before consent existed: nothing collected.
    #[serde(default)]
    consent: Consent,
}

/// What the owner agreed to (S2.6, D-11). Kept on the device and reported with every heartbeat; the agent
/// collects nothing it lacks consent for, and the coordinator refuses telemetry without it.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
struct Consent {
    idle_data: bool,
    diagnostics: bool,
    policy_version: String,
    /// When the owner last changed it (UTC).
    recorded_at: Option<String>,
}

const CONSENT_POLICY_VERSION: &str = "v0";
const DEFAULT_IDLE_AFTER_S: u64 = 600;
const DEFAULT_TELEMETRY_INTERVAL_S: u64 = 60;

impl Consent {
    fn new(idle_data: bool, diagnostics: bool) -> Self {
        Consent { idle_data, diagnostics, policy_version: CONSENT_POLICY_VERSION.into(),
                  recorded_at: Some(logging::rfc3339(SystemTime::now())) }
    }
}

fn default_digest() -> String {
    DEFAULT_BACKEND_DIGEST.to_string()
}

#[derive(Deserialize)]
struct HeartbeatReply {
    policy: OwnerPolicy,
    lease_s: u64,
    assignment: Option<serde_json::Value>,
    #[serde(default)]
    prefetch: Option<serde_json::Value>,
    /// Requests the coordinator is running on this node's deployment, for log correlation.
    #[serde(default)]
    active_request_ids: Vec<String>,
    /// Site telemetry settings (S2.3).
    #[serde(default)]
    telemetry: Option<TelemetrySettings>,
    /// Site contribution-governor settings (S4.9).
    #[serde(default)]
    contribution: Option<governor::Settings>,
    /// Diagnostic bundles an administrator asked for (S4.4).
    #[serde(default)]
    diagnostics_requests: Vec<String>,
    /// A temporary log-level window (S4.5).
    #[serde(default)]
    log_level: Option<LogWindow>,
}

#[derive(Deserialize)]
struct LogWindow {
    level: String,
    remaining_s: u64,
}

/// The state file's contents without secrets (token, keys, enrollment code), for crash reports and bundles.
fn redacted_config(st: &State, opts: &RunOptions) -> serde_json::Value {
    json!({
        "device_id": st.device_id, "name": st.name, "coordinator": st.coordinator,
        "coordinator_fingerprint": st.coordinator_fingerprint, "simulate": st.simulate,
        "owner_policy": st.owner_policy, "backend_digest": st.backend_digest, "consent": st.consent,
        "tunnel_identity": st.tunnel_cert_pem.is_some(), "worker": opts.worker.as_ref().map(|w| json!({
            "bin": w.bin.file_name().map(|n| n.to_string_lossy().into_owned()), "threads": w.threads,
            "port": w.port})),
        "tunnel": opts.tunnel.is_some(), "telemetry_buffer_bytes": opts.telemetry_buffer_bytes,
    })
}

#[derive(Deserialize)]
struct TelemetrySettings {
    interval_s: u64,
    idle_after_s: u64,
}

fn take_probe(simulate: &Option<String>) -> Result<probe::Probe, String> {
    match simulate {
        Some(name) => probe::simulated(name).ok_or_else(|| format!("unknown simulation profile {name}")),
        None => Ok(probe::real()),
    }
}

fn take_probe_cached(simulate: &Option<String>, bandwidth: f64) -> Result<probe::Probe, String> {
    match simulate {
        Some(_) => take_probe(simulate),
        None => Ok(probe::real_with(bandwidth)),
    }
}

struct RunOptions {
    beats: Option<u64>,
    worker: Option<WorkerConfig>,
    decode_probe: Option<(PathBuf, PathBuf, String)>,
    /// (listen, advertise) for the mTLS tunnel.
    tunnel: Option<(String, String)>,
    insecure_plaintext: bool,
    cache_dir: PathBuf,
    licenses: Vec<String>,
    telemetry_interval_s: Option<u64>,
    idle_after_s: Option<u64>,
    telemetry_buffer_bytes: u64,
    /// Test hook (S4.3 acceptance): panic after this many heartbeats.
    debug_panic_after_beats: Option<u64>,
}

fn capabilities(p: &probe::Probe, cap: u64, rtt_ms: f64, digest: &str, hw: Option<&hardware::Hardware>)
                -> serde_json::Value {
    let mut v = json!({
        "os": p.os, "arch": p.arch, "backend_digest": digest, "formats": ["gguf"],
        "allocatable_bytes": allocatable(cap, p.available_bytes),
        "mem_bandwidth_gbps": (p.mem_bandwidth_gbps * 10.0).round() / 10.0,
        "rtt_ms": rtt_ms, "power": p.power.label(), "battery_pct": p.power.pct(),
    });
    if let Some(h) = hw {
        v["hardware"] = json!(h);
    }
    v
}

/// The hardware profile of this machine, or none for a simulated stand-in (it would describe the wrong machine).
fn hardware_for(simulate: &Option<String>) -> Option<hardware::Hardware> {
    simulate.is_none().then(hardware::profile)
}

fn save_state(path: &Path, state: &State) -> std::io::Result<()> {
    let body = serde_json::to_vec_pretty(state)?;
    secure_fs::write_private(path, &body)
}

const CONTROL_TIMEOUT: Duration = Duration::from_secs(10);

#[allow(clippy::too_many_arguments)]
fn enroll(coordinator: String, fingerprint: Option<String>, code: String, name: String, memory_cap_gb: f64,
          allow_battery: bool, state: PathBuf, simulate: Option<String>, backend_digest: String, consent: Consent)
          -> Result<(), String> {
    // Check the address and fingerprint before probing or generating keys, so mistakes fail fast.
    let (http, seen) = control::client(&coordinator, fingerprint.as_deref(), CONTROL_TIMEOUT, CONTROL_TIMEOUT)?;
    let p = take_probe(&simulate)?;
    let owner_policy = OwnerPolicy {
        memory_cap_bytes: (memory_cap_gb * GB) as u64,
        allow_battery,
        battery_stop_pct: 30.0,
        battery_resume_pct: 40.0,
        paused: false,
        warm_hold_minutes: 30,
        max_level: 1.0,
    };
    let identity = tunnel::generate_identity(&name)?;
    let hw = hardware_for(&simulate);
    let body = json!({
        "code": code, "name": name, "public_key": identity.cert_pem,
        "capabilities": capabilities(&p, owner_policy.memory_cap_bytes, p.simulated_rtt_ms, &backend_digest,
                                     hw.as_ref()),
        "policy": owner_policy, "consent": consent,
    });
    let url = format!("{}/api/v1/devices/enroll", coordinator.trim_end_matches('/'));
    let resp = http.post(url).json(&body).send().map_err(|e| {
        if fingerprint.is_some() && seen.lock().unwrap().is_none() && e.is_connect() {
            format!("cannot reach coordinator, or its certificate does not match the fingerprint (the code was not \
                     sent): {e}")
        } else {
            format!("cannot reach coordinator: {e}")
        }
    })?;
    let status = resp.status();
    let reply: serde_json::Value = resp.json().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("enrollment failed ({status}): {}", reply["error"]["message"]));
    }
    let st = State {
        coordinator,
        device_id: reply["device_id"].as_str().unwrap_or_default().to_string(),
        device_token: reply["device_token"].as_str().unwrap_or_default().to_string(),
        name,
        simulate,
        owner_policy,
        backend_digest,
        tunnel_cert_pem: Some(identity.cert_pem),
        tunnel_key_pem: Some(identity.key_pem),
        coordinator_cert_pem: reply["coordinator_cert_pem"].as_str().map(str::to_string),
        coordinator_fingerprint: seen.lock().unwrap().clone(),
        consent,
    };
    save_state(&state, &st).map_err(|e| format!("cannot save the state file: {e}"))?;
    logging::set_node(&st.device_id);
    info!(os = %p.os, arch = %p.arch, "enrolled device {} as '{}'; state saved to {}", st.device_id, st.name,
          state.display());
    Ok(())
}

/// Put the worker behind the mTLS tunnel, or refuse to expose the plaintext worker on a network.
fn secure_worker(mut w: WorkerConfig, st: &State, opts: &RunOptions) -> Result<WorkerConfig, String> {
    match &opts.tunnel {
        Some((listen, advertise)) => {
            if !tunnel::is_loopback_host(&w.host) {
                return Err("with --tunnel-listen the worker stays on loopback; remove --worker-host".into());
            }
            if advertise.starts_with("0.0.0.0") || advertise.starts_with("[::]") {
                return Err("set --tunnel-advertise to an address the coordinator can reach".into());
            }
            let (Some(cert), Some(key), Some(coord)) = (&st.tunnel_cert_pem, &st.tunnel_key_pem, &st.coordinator_cert_pem)
            else {
                return Err("this enrollment has no tunnel certificates; enroll the device again with this agent".into());
            };
            let identity = tunnel::Identity { cert_pem: cert.clone(), key_pem: key.clone() };
            tunnel::spawn(listen, w.port, tunnel::server_config(&identity, coord)?)?;
            info!("tunnel: mutual TLS on {listen} (advertised {advertise}); worker on 127.0.0.1:{}", w.port);
            w.advertise = advertise.clone();
            w.transport = "mtls";
        }
        None if !tunnel::is_loopback_host(&w.host) && !opts.insecure_plaintext => {
            return Err(format!("refusing to expose the unencrypted llama.cpp worker on {}; use --tunnel-listen, or \
                                --insecure-plaintext-worker on an isolated lab network", w.host));
        }
        None => {}
    }
    Ok(w)
}

fn run(state_path: PathBuf, opts: RunOptions) -> Result<(), String> {
    let raw = std::fs::read(&state_path).map_err(|e| format!("cannot read {}: {e}", state_path.display()))?;
    let st: State = serde_json::from_slice(&raw)
        .map_err(|e| format!("the state file {} is not valid ({e}); enroll this device again", state_path.display()))?;
    logging::set_node(&st.device_id);
    diagnostics::install(state_dir(&state_path), redacted_config(&st, &opts));
    let (http, _) = control::client(&st.coordinator, st.coordinator_fingerprint.as_deref(), CONTROL_TIMEOUT,
                                    CONTROL_TIMEOUT)
        .map_err(|e| format!("{e} (enroll this device again with the coordinator's https address)"))?;
    let url = format!("{}/api/v1/devices/{}/heartbeat", st.coordinator.trim_end_matches('/'), st.device_id);
    let mut effective = st.owner_policy.clone();
    let mut battery_hold = false;
    let mut lease = Lease::new(Duration::from_secs(30));
    let mut last_rtt = 0.0;
    let mut rtt_window = control::RttWindow::new(5);
    let mut assignment: Option<serde_json::Value> = None;
    let mut count = 0u64;
    let bandwidth = if st.simulate.is_some() { 0.0 } else { probe::measure_bandwidth() };
    let decode = opts.decode_probe.as_ref().and_then(|(bench, model, catalog)| {
        let threads = opts.worker.as_ref().map(|w| w.threads);
        match probe::decode_probe(bench, model, threads) {
            Ok(tps) => {
                info!(tokens_per_s = tps, "decode probe: {tps:.1} tokens/s on {}",
                      if threads.is_some() { "cpu" } else { "default backend" });
                Some(json!({"model": catalog, "tokens_per_s": tps,
                            "backend": if threads.is_some() { "cpu" } else { "default" }, "threads": threads}))
            }
            Err(e) => {
                warn!(error_code = "PROBE_FAILED", "decode probe unavailable: {e}");
                None
            }
        }
    });
    let worker_cfg = match opts.worker.clone() {
        Some(w) => Some(secure_worker(w, &st, &opts)?),
        None => None,
    };
    let mut worker = worker_cfg.map(|mut w| {
        w.cache_dir = Some(opts.cache_dir.clone());
        Worker::new(w)
    });
    let mut prefetcher = prefetch::Prefetcher::new(opts.cache_dir.clone(), opts.licenses.clone(), prefetch::Source {
        coordinator: st.coordinator.clone(), device_token: st.device_token.clone(),
        fingerprint: st.coordinator_fingerprint.clone() });
    // Stop the worker on SIGTERM or Ctrl-C so no inference process outlives its supervisor.
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let flag = stop.clone();
    let _ = ctrlc::set_handler(move || flag.store(true, std::sync::atomic::Ordering::SeqCst));
    // Local policy and power decide from the start; the worker also waits for a valid lease from a heartbeat.
    let mut paused = effective_paused(&effective, take_probe_cached(&st.simulate, bandwidth)?.power, &mut battery_hold);
    let mut next_beat = Instant::now();
    // Hardware profile (S3.3, S3.4), once: it does not change while the agent runs.
    let hw = hardware_for(&st.simulate);
    if let Some(h) = &hw {
        info!(cpu = %h.cpu_model, ram_gb = h.ram_bytes / 1_000_000_000, gpus = h.gpus.len(),
              backends = %h.backends.join(","), idle_source = h.idle_source,
              "hardware: {} ({} threads), {:.0} GB RAM{}, GPUs: {}; backends: {}", h.cpu_model, h.cpu_threads,
              h.ram_bytes as f64 / 1e9, if h.unified_memory { " unified" } else { "" },
              if h.gpus.is_empty() { "none".into() } else {
                  h.gpus.iter().map(|g| g.name.clone()).collect::<Vec<_>>().join(", ") },
              h.backends.join(", "));
    }
    let mut health = health::Sampler::new(hw.as_ref().is_some_and(|h| h.backends.contains(&"cuda")));
    // Idle monitoring and telemetry (S2.1 to S2.6): idle data needs the owner's idle-data consent; health
    // samples and error counts (S3.5) need diagnostics consent. The buffer exists if either was given.
    let telemetry_dir = state_dir(&state_path).join("telemetry");
    if !st.consent.idle_data {
        info!("idle data is not collected: the owner has not consented (idlemesh-agent consent --idle-data true)");
    }
    if !st.consent.diagnostics {
        info!("health and error counts are not sent: the owner has not consented to diagnostics \
               (idlemesh-agent consent --diagnostics true)");
    }
    let mut buffer = if st.consent.idle_data || st.consent.diagnostics {
        Some(telemetry::Buffer::open(&telemetry_dir, opts.telemetry_buffer_bytes)
            .map_err(|e| format!("cannot open the telemetry buffer {}: {e}", telemetry_dir.display()))?)
    } else {
        None
    };
    let mut lid: Option<bool> = None;
    let telemetry_url = format!("{}/api/v1/devices/{}/telemetry", st.coordinator.trim_end_matches('/'), st.device_id);
    let mut interval_s = opts.telemetry_interval_s.unwrap_or(DEFAULT_TELEMETRY_INTERVAL_S);
    let mut idle_after_s = opts.idle_after_s.unwrap_or(DEFAULT_IDLE_AFTER_S);
    let mut tracker = node_state::Tracker::default();
    let mut cpu = idle::CpuSampler::new();
    let mut next_check = Instant::now();
    let mut next_sample = Instant::now();
    let mut upload_due = false;
    // Contribution governor (S4.9): site settings arrive with heartbeat replies; until then, the defaults.
    let mut gov_settings = governor::Settings { idle_after_s, ..governor::Settings::default() };
    let mut gov = governor::Governor::default();
    let max_threads = worker.as_ref().map_or(1, |w| w.threads());
    // Remote log level (S4.5): the level to return to, and when the current window ends.
    let base_level = logging::level();
    let mut log_window: Option<(String, Instant)> = None;
    // Crash reports saved by an earlier run (S4.3) go up with the first heartbeat that gets through.
    let mut crashes_due = st.consent.diagnostics && !diagnostics::pending().is_empty();
    let mut next_crash_try = Instant::now();
    loop {
        if stop.load(std::sync::atomic::Ordering::SeqCst) {
            if let Some(w) = worker.as_mut() {
                w.halt("agent stopped");
            }
            info!("agent stopping: worker halted");
            return Ok(());
        }
        // Watchdog: every 250 ms, independent of heartbeats and coordinator reachability.
        if let Some(w) = worker.as_mut() {
            let should_run = !paused && lease.valid(Instant::now());
            if w.tick(should_run, effective.memory_cap_bytes) == worker::Change::Killed {
                next_beat = Instant::now(); // report the capacity change immediately
            }
            if let Some((status, tail)) = w.take_crash() {
                diagnostics::save_worker_crash(&status, &tail);
                crashes_due = st.consent.diagnostics;
                next_crash_try = Instant::now();
            }
        }
        // A remote log-level window ends on time even if the coordinator is unreachable (S4.5).
        if log_window.as_ref().is_some_and(|(_, until)| Instant::now() >= *until) {
            end_log_window(&mut log_window, base_level, "window expired");
        }
        if Instant::now() >= next_check {
            next_check = Instant::now() + Duration::from_secs(1);
            let reading = idle::read();
            let now = SystemTime::now();
            let current = node_state::classify(&node_state::Inputs {
                seconds_since_input: reading.seconds_since_input, session_locked: reading.session_locked,
                idle_after_s, assigned: assignment.is_some(), paused });
            // Governor (S4.9): every second, so a returning user gets the machine back within about a second.
            let (weekday, minute) = governor::Inputs::now_local();
            let level = governor::decide(&gov_settings, &governor::Inputs {
                seconds_since_input: reading.seconds_since_input, session_locked: reading.session_locked,
                owner_paused: effective.paused, power_paused: paused && !effective.paused,
                max_level: effective.max_level, weekday, minute });
            if let Some((prev, next)) = gov.update(level) {
                info!(level = next.level, reason = next.reason.as_str(), ceiling = next.ceiling,
                      "contribution level {:.2} -> {:.2} ({})", prev.map_or(0.0, |p| p.level), next.level,
                      next.reason.as_str());
            }
            if let Some(w) = worker.as_mut() {
                w.apply_level(level.level);
                w.set_threads(governor::threads(max_threads, gov_settings.max_cpu_fraction, level.ceiling));
            }
            let idle_buf = buffer.as_mut().filter(|_| st.consent.idle_data);
            if let Some(b) = idle_buf {
                for ch in tracker.observe(now, current) {
                    let from = ch.from.map_or("none", |s| s.as_str());
                    info!(from, to = ch.to.as_str(), "node state: {from} -> {}", ch.to.as_str());
                    record(b, json!({"kind": "state", "ts": logging::rfc3339(ch.at), "from": ch.from, "to": ch.to}));
                    upload_due = true;
                }
                if reading.lid_closed.is_some() && reading.lid_closed != lid {
                    if lid.is_some() {
                        info!(lid_closed = reading.lid_closed, "lid {}",
                              if reading.lid_closed == Some(true) { "closed" } else { "opened" });
                    }
                    record(b, json!({"kind": "lid", "ts": logging::rfc3339(now), "closed": reading.lid_closed}));
                    lid = reading.lid_closed;
                    upload_due = true;
                }
            } else {
                tracker.observe(now, current); // the node state is still reported in heartbeats
            }
            if Instant::now() >= next_sample {
                next_sample = Instant::now() + Duration::from_secs(interval_s);
                if let Some(b) = buffer.as_mut() {
                    let ts = logging::rfc3339(now);
                    let gpu = health.gpu();
                    if st.consent.idle_data {
                        let p = take_probe_cached(&st.simulate, bandwidth)?;
                        record(b, json!({
                            "kind": "idle", "ts": ts,
                            "seconds_since_input": reading.seconds_since_input, "session_locked": reading.session_locked,
                            "cpu_util": cpu.sample(), "gpu_util": gpu.as_ref().map(|g| (g.util * 1000.0).round() / 1000.0),
                            "mem_free_gb": (p.available_bytes as f64 / GB * 100.0).round() / 100.0,
                            "on_battery": matches!(p.power, policy::Power::Battery(_)), "battery_pct": p.power.pct(),
                            "state": current, "contribution_level": level.level,
                            "level_reason": level.reason.as_str(),
                        }));
                    }
                    if st.consent.diagnostics {
                        let counters = health::Counters {
                            worker_restarts: worker.as_ref().map_or(0, |w| w.restarts),
                            cap_kills: worker.as_ref().map_or(0, |w| w.cap_kills),
                            tunnel_errors: logging::error_total("TUNNEL_"),
                            telemetry_buffer_bytes: b.bytes(), telemetry_dropped: b.dropped() };
                        record(b, health.sample(&ts, &opts.cache_dir, gpu.as_ref(), counters));
                        let errors = logging::take_error_counts();
                        if !errors.is_empty() {
                            record(b, json!({"kind": "errors", "ts": ts, "counts": errors}));
                        }
                    }
                    upload_due = true;
                }
            }
        }
        if Instant::now() >= next_beat {
            let p = take_probe_cached(&st.simulate, bandwidth)?;
            paused = effective_paused(&effective, p.power, &mut battery_hold);
            let rtt = if st.simulate.is_some() { p.simulated_rtt_ms } else { last_rtt };
            // The memory budget offered follows the governor's ceiling (time window, owner limit), not the
            // momentary yield: memory is given back at handoff, not the moment a user touches the mouse (D-12).
            let ceiling = gov.current.map_or(1.0, |l| l.ceiling);
            let budget = (effective.memory_cap_bytes as f64 * ceiling) as u64;
            let mut caps = capabilities(&p, budget, rtt, &st.backend_digest, hw.as_ref());
            if let Some(d) = &decode {
                caps["decode_probe"] = d.clone();
            }
            let mut body = json!({"capabilities": caps, "paused": paused, "consent": st.consent,
                                  "node_state": tracker.state(),
                                  "log_level": logging::level().map(|l| l.to_string().to_lowercase())});
            if let Some(l) = gov.current {
                body["contribution"] = json!({"level": l.level, "reason": l.reason.as_str()});
            }
            if let Some(b) = buffer.as_ref() {
                body["telemetry"] = json!({"buffer_bytes": b.bytes(), "dropped": b.dropped()});
            }
            if let Some(w) = worker.as_mut() {
                body["worker"] = w.status();
            }
            if let Some(s) = prefetcher.status() {
                body["prefetch"] = s.json();
            }
            let started = Instant::now();
            let trace_id = logging::new_trace_id();
            match http.post(&url).bearer_auth(&st.device_token).header("X-Trace-Id", &trace_id).json(&body).send() {
                Ok(resp) if resp.status().is_success() => {
                    let server_ms = control::server_time_ms(
                        resp.headers().get("server-timing").and_then(|v| v.to_str().ok()));
                    last_rtt = rtt_window.push(started.elapsed().as_secs_f64() * 1000.0, server_ms);
                    let reply: HeartbeatReply = resp.json()
                        .map_err(|e| format!("the coordinator sent an invalid heartbeat reply: {e}"))?;
                    effective = merge(&st.owner_policy, &reply.policy);
                    paused = effective_paused(&effective, p.power, &mut battery_hold);
                    lease.renew(Instant::now(), Duration::from_secs(reply.lease_s));
                    if let Some(t) = &reply.telemetry {
                        let interval = opts.telemetry_interval_s.unwrap_or(t.interval_s).max(1);
                        if interval != interval_s {
                            next_sample = next_sample.min(Instant::now() + Duration::from_secs(interval));
                            interval_s = interval;
                        }
                        idle_after_s = opts.idle_after_s.unwrap_or(t.idle_after_s);
                    }
                    if let Some(c) = reply.contribution.clone() {
                        gov_settings = governor::Settings { idle_after_s, ..c };
                    }
                    if crashes_due && Instant::now() >= next_crash_try {
                        diagnostics::upload_pending(&http, &st.coordinator, &st.device_id, &st.device_token);
                        crashes_due = !diagnostics::pending().is_empty();
                        next_crash_try = Instant::now() + Duration::from_secs(300); // what is left: retry later
                    }
                    match (&reply.log_level, &log_window) {
                        (Some(w), current) if st.consent.diagnostics => {
                            let until = Instant::now() + Duration::from_secs(w.remaining_s);
                            if current.as_ref().is_none_or(|(lvl, _)| *lvl != w.level) {
                                match logging::parse_level(&w.level).and_then(logging::set_level) {
                                    Ok(()) => info!(log_level = %w.level, minutes = w.remaining_s / 60,
                                                    "log level raised to {} for {} min (requested by the coordinator)",
                                                    w.level, w.remaining_s.div_ceil(60)),
                                    Err(e) => warn!(error_code = "CONFIG_INVALID", "cannot change the log level: {e}"),
                                }
                            }
                            log_window = Some((w.level.clone(), until));
                        }
                        (None, Some(_)) => end_log_window(&mut log_window, base_level, "closed by the coordinator"),
                        _ => {}
                    }
                    for bundle_id in reply.diagnostics_requests.iter().take(1) {
                        if !st.consent.diagnostics {
                            break;
                        }
                        let sections = json!({
                            "capabilities": body["capabilities"], "worker": body.get("worker"),
                            "telemetry": body.get("telemetry"), "contribution": body.get("contribution"),
                            "node_state": tracker.state(), "lease_valid": lease.valid(Instant::now()),
                            "assignment": assignment, "rtt_ms": last_rtt,
                            "health": st.consent.diagnostics.then(|| health.sample(
                                &logging::rfc3339(SystemTime::now()), &opts.cache_dir, health.gpu().as_ref(),
                                health::Counters { worker_restarts: worker.as_ref().map_or(0, |w| w.restarts),
                                    cap_kills: worker.as_ref().map_or(0, |w| w.cap_kills),
                                    tunnel_errors: logging::error_total("TUNNEL_"),
                                    telemetry_buffer_bytes: buffer.as_ref().map_or(0, |b| b.bytes()),
                                    telemetry_dropped: buffer.as_ref().map_or(0, |b| b.dropped()) })),
                            "idle": idle::read().source,
                        });
                        let bundle = diagnostics::bundle(sections);
                        match diagnostics::send_bundle(&http, &st.coordinator, &st.device_id, &st.device_token,
                                                       bundle_id, &bundle) {
                            Ok(()) => info!(bundle_id = %bundle_id, size_bytes = bundle.to_string().len(),
                                            "diagnostic bundle uploaded"),
                            Err(e) => warn!(error_code = "DIAGNOSTICS_FAILED", bundle_id = %bundle_id,
                                            "diagnostic bundle not delivered: {e}"),
                        }
                    }
                    if upload_due {
                        if let Some(b) = buffer.as_mut() {
                            let sent = b.flush(|batch| send_telemetry(&http, &telemetry_url, &st.device_token, batch));
                            upload_due = !b.is_empty();
                            if sent > 0 {
                                debug!(records = sent, "telemetry uploaded: {sent} records");
                            }
                        }
                    }
                    if let Some(m) = reply.prefetch.as_ref() {
                        prefetcher.offer(m);
                    }
                    if reply.assignment != assignment {
                        logging::set_deployment(reply.assignment.as_ref().and_then(|a| a["deployment_id"].as_str()));
                        info!(trace_id = %trace_id, "assignment: {}",
                              reply.assignment.as_ref().map_or("none".into(), |a| a.to_string()));
                        assignment = reply.assignment;
                    }
                    if logging::set_requests(reply.active_request_ids.clone()) {
                        info!(trace_id = %trace_id, "serving requests: {}",
                              if reply.active_request_ids.is_empty() { "none".into() }
                              else { reply.active_request_ids.join(", ") });
                    }
                    info!(trace_id = %trace_id, paused, rtt_ms = last_rtt,
                          "heartbeat ok: paused={paused} power={} cap={:.2}GB rtt={last_rtt}ms",
                          p.power.label(), effective.memory_cap_bytes as f64 / GB);
                }
                Ok(resp) if resp.status().as_u16() == 401 || resp.status().as_u16() == 403 => {
                    error!(error_code = "DEVICE_REVOKED", trace_id = %trace_id,
                           "device authorization revoked: stopping all local work");
                    if let Some(w) = worker.as_mut() {
                        w.halt("authorization revoked");
                    }
                    return Err("revoked".into());
                }
                Ok(resp) => warn!(error_code = "HEARTBEAT_REJECTED", trace_id = %trace_id,
                                  "heartbeat rejected: {}", resp.status()),
                Err(e) => warn!(error_code = "CONTROL_UNREACHABLE", trace_id = %trace_id,
                                "coordinator unreachable: {e}"),
            }
            if assignment.is_some() && !lease.valid(Instant::now()) {
                warn!(error_code = "LEASE_EXPIRED", "lease expired: releasing assigned work");
                assignment = None;
                logging::set_deployment(None);
                logging::set_requests(Vec::new());
            }
            count += 1;
            if opts.debug_panic_after_beats.is_some_and(|n| count >= n) {
                panic!("debug panic requested with --debug-panic-after-beats {count}");
            }
            if opts.beats.is_some_and(|b| count >= b) {
                return Ok(());
            }
            next_beat = Instant::now() + Duration::from_secs(5);
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

fn end_log_window(window: &mut Option<(String, Instant)>, base: Option<tracing_subscriber::filter::LevelFilter>,
                  why: &str) {
    if window.take().is_some() {
        if let Some(b) = base {
            let _ = logging::set_level(b);
        }
        info!("log level back to {} ({why})", base.map_or("default".into(), |b| b.to_string().to_lowercase()));
    }
}

fn state_dir(state: &Path) -> &Path {
    state.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."))
}

fn record(b: &mut telemetry::Buffer, v: serde_json::Value) {
    if let Err(e) = b.push(&v) {
        error!(error_code = "TELEMETRY_BUFFER_IO", "cannot write to the telemetry buffer: {e}");
    }
}

/// One telemetry batch to the coordinator. A batch it calls invalid is dropped; anything else is retried.
fn send_telemetry(http: &reqwest::blocking::Client, url: &str, token: &str, batch: &serde_json::Value)
                  -> Result<(), telemetry::SendError> {
    let resp = http.post(url).bearer_auth(token).header("X-Trace-Id", logging::new_trace_id()).json(batch).send()
        .map_err(|e| telemetry::SendError::Retry(e.to_string()))?;
    let status = resp.status();
    let detail = || format!("{status}: {}", resp.text().unwrap_or_default());
    match status.as_u16() {
        200..=299 => Ok(()),
        400 | 413 | 422 => Err(telemetry::SendError::Invalid(detail())),
        _ => Err(telemetry::SendError::Retry(detail())),
    }
}

fn consent_cmd(state: PathBuf, idle_data: Option<bool>, diagnostics: Option<bool>) -> Result<(), String> {
    let raw = std::fs::read(&state).map_err(|e| format!("cannot read {}: {e}", state.display()))?;
    let mut st: State = serde_json::from_slice(&raw).map_err(|e| format!("{} is not valid: {e}", state.display()))?;
    if idle_data.is_some() || diagnostics.is_some() {
        st.consent = Consent::new(idle_data.unwrap_or(st.consent.idle_data),
                                  diagnostics.unwrap_or(st.consent.diagnostics));
        save_state(&state, &st).map_err(|e| format!("cannot save the state file: {e}"))?;
        info!(idle_data = st.consent.idle_data, diagnostics = st.consent.diagnostics, "consent changed");
    }
    println!("{}", serde_json::to_string_pretty(&st.consent).unwrap());
    Ok(())
}

fn idle_cmd(watch: bool, idle_after_s: u64) -> Result<(), String> {
    let mut cpu = idle::CpuSampler::new();
    loop {
        std::thread::sleep(Duration::from_secs(1));
        let r = idle::read();
        let state = node_state::classify(&node_state::Inputs {
            seconds_since_input: r.seconds_since_input, session_locked: r.session_locked, idle_after_s,
            assigned: false, paused: false });
        println!("{}", json!({"seconds_since_input": r.seconds_since_input, "session_locked": r.session_locked,
                               "lid_closed": r.lid_closed, "source": r.source, "cpu_util": cpu.sample(),
                               "power": probe::real_with(0.0).power.label(), "state": state}));
        if !watch {
            return Ok(());
        }
    }
}

/// The JSON log goes next to the state file unless --log-dir says otherwise; `probe` and `tunnel` have no
/// state file and log to the console only unless --log-dir is given.
fn log_options(cli: &Cli) -> Result<logging::Options, String> {
    let state_logs = |state: &Path| state_dir(state).join("logs");
    let dir = cli.log_dir.clone().or_else(|| match &cli.cmd {
        Cmd::Enroll { state, .. } | Cmd::Run { state, .. } | Cmd::Consent { state, .. } => Some(state_logs(state)),
        _ => None,
    });
    Ok(logging::Options { level: logging::parse_level(&cli.log_level)?, dir, max_bytes: cli.log_max_mb * 1_000_000,
                          keep: cli.log_keep })
}

fn main() {
    let mut cli = Cli::parse();
    // `run --config` (S4.2): the config names the state file, so resolve it before logging picks its directory.
    let mut node_cfg = None;
    if let Cmd::Run { config: Some(path), state, .. } = &mut cli.cmd {
        match node_config::NodeConfig::load(path) {
            Ok(c) => {
                *state = c.state_path(path);
                node_cfg = Some(c);
            }
            Err(e) => {
                eprintln!("idlemesh-agent: {e}");
                std::process::exit(1);
            }
        }
    }
    if let Err(e) = log_options(&cli).and_then(|o| logging::init(&o)) {
        eprintln!("idlemesh-agent: {e}");
        std::process::exit(1);
    }
    info!(target: logging::FILE_ONLY, version = env!("CARGO_PKG_VERSION"), os = std::env::consts::OS,
          "agent starting");
    let result = match cli.cmd {
        Cmd::Enroll { coordinator, coordinator_fingerprint, code, name, memory_cap_gb, allow_battery, state, simulate,
                      backend_digest, consent_idle_data, consent_diagnostics } => {
            enroll(coordinator, coordinator_fingerprint, code, name, memory_cap_gb, allow_battery, state, simulate,
                   backend_digest, Consent::new(consent_idle_data, consent_diagnostics))
        }
        Cmd::Consent { state, idle_data, diagnostics } => consent_cmd(state, idle_data, diagnostics),
        Cmd::Idle { watch, idle_after_s } => idle_cmd(watch, idle_after_s),
        Cmd::Run { state, config: _, debug_panic_after_beats, beats, worker_bin, worker_host, worker_port,
                   worker_threads, decode_probe_bench, decode_probe_model, decode_probe_catalog, tunnel_listen,
                   tunnel_advertise, insecure_plaintext_worker, cache_dir, accept_licenses, telemetry_interval_s,
                   idle_after_s, telemetry_buffer_mb } => (|| {
            if let Some(c) = node_cfg.filter(|_| !state.exists()) {
                let code = c.enrollment_code.clone()
                    .ok_or("the node config has no enrollment_code and this node is not enrolled yet")?;
                info!("enrolling from the node config (first start)");
                enroll(c.coordinator, c.coordinator_fingerprint, code, c.name, c.memory_cap_gb, c.allow_battery,
                       state.clone(), None, c.backend_digest.unwrap_or_else(default_digest),
                       Consent::new(c.consent.idle_data, c.consent.diagnostics))?;
            }
            let cache_dir = cache_dir.unwrap_or_else(|| state_dir(&state).join("difa-cache"));
            let worker = worker_bin.map(|bin| WorkerConfig {
                bin, advertise: format!("{worker_host}:{worker_port}"), host: worker_host, port: worker_port,
                threads: worker_threads, transport: "plaintext", cache_dir: None });
            let decode_probe = decode_probe_bench.zip(decode_probe_model).map(|(b, m)| (b, m, decode_probe_catalog));
            let tunnel = tunnel_listen.map(|l| (tunnel_advertise.unwrap_or_else(|| l.clone()), l)).map(|(a, l)| (l, a));
            run(state, RunOptions { beats, worker, decode_probe, tunnel, insecure_plaintext: insecure_plaintext_worker,
                                    cache_dir, licenses: accept_licenses, telemetry_interval_s, idle_after_s,
                                    telemetry_buffer_bytes: telemetry_buffer_mb * 1_000_000,
                                    debug_panic_after_beats })
        })(),
        Cmd::AllocTest { mb } => alloc_test(mb),
        Cmd::LimitTest { cap_mb, alloc_mb } => limit_test(cap_mb, alloc_mb),
        Cmd::Tunnel { listen, worker_port, cert, key, peer_cert } => (|| {
            let read = |p: &PathBuf| std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()));
            let identity = tunnel::Identity { cert_pem: read(&cert)?, key_pem: read(&key)? };
            tunnel::spawn(&listen, worker_port, tunnel::server_config(&identity, &read(&peer_cert)?)?)?;
            info!("tunnel: mutual TLS on {listen} -> 127.0.0.1:{worker_port}");
            let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
            let flag = stop.clone();
            let _ = ctrlc::set_handler(move || flag.store(true, std::sync::atomic::Ordering::SeqCst));
            while !stop.load(std::sync::atomic::Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(200));
            }
            Ok(())
        })(),
        Cmd::Probe { simulate } => take_probe(&simulate).map(|p| {
            let caps = capabilities(&p, u64::MAX, p.simulated_rtt_ms, DEFAULT_BACKEND_DIGEST,
                                    hardware_for(&simulate).as_ref());
            println!("{}", serde_json::to_string_pretty(&caps).unwrap());
        }),
    };
    if let Err(e) = result {
        error!(target: logging::FILE_ONLY, "exiting with error: {e}");
        eprintln!("idlemesh-agent: {e}");
        std::process::exit(if e == "revoked" { 2 } else { 1 });
    }
}
