//! Node health samples (S3.5; PROJECT_PLAN §6 NodeHealthSample).
//!
//! One sample per telemetry interval: CPU and GPU use, memory, temperature where the OS exposes it, free disk
//! for the model cache, and the agent's counters. Counters (worker restarts, cap kills, tunnel errors) count
//! from the agent's start and are sent with `agent_uptime_s`, so a reset is always visible as the uptime
//! starting again, never silent. GPU figures come from `nvidia-smi` (one query per sample, read only);
//! nodes without an NVIDIA GPU report them as absent rather than zero.

use serde_json::{json, Value};
use std::path::Path;
use std::process::Command;
use std::time::Instant;
use sysinfo::{Components, Disks, System};

#[derive(Clone, Debug, Default, PartialEq)]
pub struct GpuNow {
    pub util: f64,
    pub mem_used_bytes: u64,
    pub temp_c: Option<f64>,
}

/// Counters owned by other parts of the agent.
#[derive(Clone, Copy, Debug, Default)]
pub struct Counters {
    pub worker_restarts: u32,
    pub cap_kills: u32,
    pub tunnel_errors: u64,
    pub telemetry_buffer_bytes: u64,
    pub telemetry_dropped: u64,
}

pub struct Sampler {
    sys: System,
    components: Components,
    started: Instant,
    nvidia: bool,
}

impl Sampler {
    pub fn new(has_nvidia: bool) -> Self {
        let mut sys = System::new();
        sys.refresh_cpu_usage();
        Sampler { sys, components: Components::new_with_refreshed_list(), started: Instant::now(), nvidia: has_nvidia }
    }

    /// GPU use across NVIDIA GPUs (mean utilisation, summed memory, hottest temperature).
    pub fn gpu(&self) -> Option<GpuNow> {
        if !self.nvidia {
            return None;
        }
        let out = Command::new("nvidia-smi")
            .args(["--query-gpu=utilization.gpu,memory.used,temperature.gpu", "--format=csv,noheader,nounits"])
            .output().ok().filter(|o| o.status.success())?;
        parse_gpu_now(&String::from_utf8_lossy(&out.stdout))
    }

    pub fn sample(&mut self, ts: &str, cache_dir: &Path, gpu: Option<&GpuNow>, c: Counters) -> Value {
        self.sys.refresh_cpu_usage();
        self.sys.refresh_memory();
        self.components.refresh(true);
        let cpu = (f64::from(self.sys.global_cpu_usage()) / 100.0).clamp(0.0, 1.0);
        let cpu_temp = self.components.list().iter().filter_map(|c| c.temperature()).map(f64::from)
            .filter(|t| t.is_finite() && *t > 0.0 && *t < 150.0).fold(None, |m: Option<f64>, t| Some(m.map_or(t, |m| m.max(t))));
        let temp = [cpu_temp, gpu.and_then(|g| g.temp_c)].into_iter().flatten().fold(None, |m: Option<f64>, t| {
            Some(m.map_or(t, |m| m.max(t)))
        });
        json!({
            "kind": "health", "ts": ts,
            "cpu_util": round3(cpu),
            "gpu_util": gpu.map(|g| round3(g.util)),
            "mem_used_gb": round2(self.sys.used_memory() as f64 / 1e9),
            "gpu_mem_used_gb": gpu.map(|g| round2(g.mem_used_bytes as f64 / 1e9)),
            "temp_c": temp.map(|t| (t * 10.0).round() / 10.0),
            "cache_disk_free_gb": disk_free(cache_dir).map(|b| round2(b as f64 / 1e9)),
            "agent_uptime_s": self.started.elapsed().as_secs(),
            "worker_restarts": c.worker_restarts, "cap_kills": c.cap_kills, "tunnel_errors": c.tunnel_errors,
            "telemetry_buffer_bytes": c.telemetry_buffer_bytes, "telemetry_dropped": c.telemetry_dropped,
        })
    }
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

fn round3(v: f64) -> f64 {
    (v * 1000.0).round() / 1000.0
}

/// `nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu --format=csv,noheader,nounits`.
pub fn parse_gpu_now(out: &str) -> Option<GpuNow> {
    let rows: Vec<(f64, u64, Option<f64>)> = out.lines().filter_map(|l| {
        let cols: Vec<&str> = l.split(',').map(str::trim).collect();
        let [util, mem, temp] = cols[..] else { return None };
        Some((util.parse::<f64>().ok()? / 100.0, mem.parse::<u64>().ok()? * 1024 * 1024, temp.parse().ok()))
    }).collect();
    if rows.is_empty() {
        return None;
    }
    Some(GpuNow {
        util: rows.iter().map(|r| r.0).sum::<f64>() / rows.len() as f64,
        mem_used_bytes: rows.iter().map(|r| r.1).sum(),
        temp_c: rows.iter().filter_map(|r| r.2).fold(None, |m: Option<f64>, t| Some(m.map_or(t, |m| m.max(t)))),
    })
}

/// Free space on the disk holding `dir` (the longest mount point that contains it). The directory need not
/// exist yet: its nearest existing ancestor decides.
pub fn disk_free(dir: &Path) -> Option<u64> {
    let mut p = dir.to_path_buf();
    while !p.exists() {
        p = p.parent()?.to_path_buf();
    }
    let p = std::fs::canonicalize(&p).unwrap_or(p);
    let disks = Disks::new_with_refreshed_list();
    disks.iter().filter(|d| starts_with(&p, d.mount_point())).max_by_key(|d| d.mount_point().as_os_str().len())
        .map(|d| d.available_space())
}

/// Path prefix test that also matches `\\?\C:\x` against the mount point `C:\`.
fn starts_with(p: &Path, mount: &Path) -> bool {
    let norm = |x: &Path| x.to_string_lossy().trim_start_matches(r"\\?\").to_ascii_lowercase();
    norm(p).starts_with(&norm(mount))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gpu_rows_are_combined() {
        let g = parse_gpu_now("10, 2048, 45\n30, 1024, 61\n").unwrap();
        assert!((g.util - 0.2).abs() < 1e-9);
        assert_eq!(g.mem_used_bytes, 3072 * 1024 * 1024);
        assert_eq!(g.temp_c, Some(61.0));
        assert_eq!(parse_gpu_now("5, 100, [N/A]").unwrap().temp_c, None);
        assert!(parse_gpu_now("").is_none());
    }

    #[test]
    fn a_sample_has_every_field_and_counters_pass_through() {
        let mut s = Sampler::new(false);
        let dir = std::env::temp_dir().join("idlemesh-health-test-does-not-exist").join("cache");
        let c = Counters { worker_restarts: 2, cap_kills: 1, tunnel_errors: 3, telemetry_buffer_bytes: 10,
                           telemetry_dropped: 0 };
        let v = s.sample("2026-11-20T00:00:00.000+00:00", &dir, None, c);
        assert_eq!(v["kind"], "health");
        assert!(v["cpu_util"].as_f64().unwrap() <= 1.0);
        assert!(v["mem_used_gb"].as_f64().unwrap() > 0.0);
        assert!(v["gpu_util"].is_null() && v["gpu_mem_used_gb"].is_null()); // absent, not zero
        assert!(v["cache_disk_free_gb"].as_f64().unwrap() > 0.0, "{v}");
        assert_eq!((v["worker_restarts"].as_u64(), v["cap_kills"].as_u64(), v["tunnel_errors"].as_u64()),
                   (Some(2), Some(1), Some(3)));
    }
}
