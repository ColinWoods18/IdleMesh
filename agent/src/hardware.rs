//! Hardware profile and backend detection (S3.3, S3.4).
//!
//! Taken once at startup and sent with every heartbeat so the coordinator's device registry shows what each
//! node is: CPU, RAM (and whether it is unified with the GPU), GPUs with their memory, and the inference
//! backends the node could run (CPU always; CUDA with an NVIDIA driver; Metal on Macs; Vulkan when a Vulkan
//! loader is installed). NVIDIA GPUs are read with `nvidia-smi` (the same data as NVML) on every OS, Macs
//! with `system_profiler`, and other GPUs from the OS (Windows CIM, Linux sysfs). Every parser is a pure
//! function over the tool's output, so it is tested on any machine.

use serde::Serialize;
use std::process::Command;
use sysinfo::System;

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Gpu {
    /// nvidia, apple, amd, intel or other.
    pub vendor: String,
    pub name: String,
    /// Dedicated memory; None for unified memory (it is the RAM) or when the OS cannot tell.
    pub vram_bytes: Option<u64>,
    pub driver: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Hardware {
    pub cpu_model: String,
    pub cpu_cores: Option<usize>,
    pub cpu_threads: usize,
    pub ram_bytes: u64,
    /// The GPU shares the system RAM (Apple Silicon): model memory is RAM, not VRAM.
    pub unified_memory: bool,
    pub gpus: Vec<Gpu>,
    /// Inference backends this node can run, best first.
    pub backends: Vec<&'static str>,
    /// Where idle readings come from (win32, iokit, logind, tty), so a wrong reading can be traced.
    pub idle_source: &'static str,
}

pub fn profile() -> Hardware {
    let mut sys = System::new();
    sys.refresh_memory();
    sys.refresh_cpu_all();
    let cpu_model = sys.cpus().first().map(|c| c.brand().trim().to_string()).filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".into());
    let mut gpus = run("nvidia-smi", &["--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
        .map(|out| parse_nvidia_smi(&out)).unwrap_or_default();
    let has_nvidia = !gpus.is_empty();
    gpus.extend(os_gpus().into_iter().filter(|g| g.vendor != "nvidia" || !has_nvidia));
    let unified_memory = cfg!(target_os = "macos") && gpus.iter().any(|g| g.vendor == "apple");
    let backends = backends(&gpus, vulkan_loader_present());
    Hardware { cpu_model, cpu_cores: sys.physical_core_count(), cpu_threads: sys.cpus().len(),
               ram_bytes: sys.total_memory(), unified_memory, gpus, backends, idle_source: crate::idle::read().source }
}

/// The backends a node can run, best first. CUDA needs an NVIDIA GPU with a working driver (nvidia-smi
/// answered); Metal needs a Mac GPU; Vulkan needs the loader and a GPU it could use.
pub fn backends(gpus: &[Gpu], vulkan_loader: bool) -> Vec<&'static str> {
    let mut out = Vec::new();
    if gpus.iter().any(|g| g.vendor == "nvidia" && g.driver.is_some()) {
        out.push("cuda");
    }
    if cfg!(target_os = "macos") && !gpus.is_empty() {
        out.push("metal");
    }
    if vulkan_loader && !gpus.is_empty() {
        out.push("vulkan");
    }
    out.push("cpu");
    out
}

fn run(cmd: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(cmd).args(args).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

/// `nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits` (memory in MiB).
pub fn parse_nvidia_smi(out: &str) -> Vec<Gpu> {
    out.lines().filter_map(|l| {
        let cols: Vec<&str> = l.split(',').map(str::trim).collect();
        let [name, mib, driver] = cols[..] else { return None };
        (!name.is_empty()).then(|| Gpu {
            vendor: "nvidia".into(), name: name.into(),
            vram_bytes: mib.parse::<u64>().ok().map(|m| m * 1024 * 1024),
            driver: (!driver.is_empty() && driver != "[N/A]").then(|| driver.into()),
        })
    }).collect()
}

fn vendor_of(name: &str) -> &'static str {
    let n = name.to_ascii_lowercase();
    if n.contains("nvidia") || n.contains("geforce") || n.contains("quadro") || n.contains("rtx") {
        "nvidia"
    } else if n.contains("apple") {
        "apple"
    } else if n.contains("amd") || n.contains("radeon") {
        "amd"
    } else if n.contains("intel") {
        "intel"
    } else {
        "other"
    }
}

/// `system_profiler SPDisplaysDataType -json` on a Mac.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn parse_system_profiler(json: &str) -> Vec<Gpu> {
    let v: serde_json::Value = serde_json::from_str(json).unwrap_or_default();
    v["SPDisplaysDataType"].as_array().map(|items| items.iter().filter_map(|g| {
        let name = g["sppci_model"].as_str()?.to_string();
        let vram = g["spdisplays_vram"].as_str().or_else(|| g["spdisplays_vram_shared"].as_str())
            .and_then(parse_size);
        let vendor = vendor_of(&name);
        Some(Gpu { vendor: vendor.into(), vram_bytes: if vendor == "apple" { None } else { vram }, name,
                   driver: g["spdisplays_mtlgpufamilysupport"].as_str().map(|s| s.replace("spdisplays_", "")) })
    }).collect()).unwrap_or_default()
}

/// "8 GB" or "1536 MB" into bytes.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn parse_size(s: &str) -> Option<u64> {
    let mut it = s.split_whitespace();
    let n: f64 = it.next()?.parse().ok()?;
    let mult = match it.next()?.to_ascii_uppercase().as_str() {
        "GB" => 1u64 << 30,
        "MB" => 1 << 20,
        _ => return None,
    };
    Some((n * mult as f64) as u64)
}

/// `Get-CimInstance Win32_VideoController | Select Name,AdapterRAM,DriverVersion | ConvertTo-Json` on
/// Windows. AdapterRAM is a 32-bit field that tops out at 4 GiB, so NVIDIA memory comes from nvidia-smi.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn parse_cim_video(json: &str) -> Vec<Gpu> {
    let v: serde_json::Value = serde_json::from_str(json.trim()).unwrap_or_default();
    let items = match v {
        serde_json::Value::Array(a) => a,
        serde_json::Value::Object(_) => vec![v],
        _ => Vec::new(),
    };
    items.iter().filter_map(|g| {
        let name = g["Name"].as_str()?.trim().to_string();
        let vendor = vendor_of(&name);
        if name.is_empty() || name.contains("Basic Display") || name.contains("Remote Display") {
            return None;
        }
        Some(Gpu { vendor: vendor.into(), name, vram_bytes: g["AdapterRAM"].as_u64().filter(|b| *b > 0),
                   driver: g["DriverVersion"].as_str().map(str::to_string) })
    }).collect()
}

/// A Linux DRM device from its PCI vendor ID (sysfs). Virtual displays (QEMU, VMware) are not GPUs.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn linux_gpu(vendor_id: &str, device_id: &str, vram: Option<u64>) -> Option<Gpu> {
    let vendor = match vendor_id.trim() {
        "0x10de" => "nvidia",
        "0x1002" => "amd",
        "0x8086" => "intel",
        _ => return None,
    };
    Some(Gpu { vendor: vendor.into(), name: format!("{} GPU {}", vendor.to_uppercase(), device_id.trim()),
               vram_bytes: vram, driver: None })
}

#[cfg(windows)]
fn os_gpus() -> Vec<Gpu> {
    run("powershell", &["-NoProfile", "-NonInteractive", "-Command",
                        "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json"])
        .map(|out| parse_cim_video(&out)).unwrap_or_default()
}

#[cfg(target_os = "macos")]
fn os_gpus() -> Vec<Gpu> {
    run("system_profiler", &["SPDisplaysDataType", "-json"]).map(|out| parse_system_profiler(&out)).unwrap_or_default()
}

#[cfg(target_os = "linux")]
fn os_gpus() -> Vec<Gpu> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir("/sys/class/drm") else { return out };
    let mut seen = std::collections::HashSet::new();
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if !name.starts_with("card") || name.contains('-') {
            continue; // card0-HDMI-A-1 and the like are connectors, not devices
        }
        let dev = e.path().join("device");
        let read = |f: &str| std::fs::read_to_string(dev.join(f)).ok();
        let (Some(vendor), Some(device)) = (read("vendor"), read("device")) else { continue };
        if !seen.insert((vendor.clone(), device.clone(), read("uevent"))) {
            continue;
        }
        let vram = read("mem_info_vram_total").and_then(|s| s.trim().parse().ok());
        out.extend(linux_gpu(&vendor, &device, vram));
    }
    out
}

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
fn os_gpus() -> Vec<Gpu> {
    Vec::new()
}

fn vulkan_loader_present() -> bool {
    let candidates: &[&str] = if cfg!(windows) {
        &[r"C:\Windows\System32\vulkan-1.dll"]
    } else if cfg!(target_os = "macos") {
        &["/usr/local/lib/libvulkan.1.dylib", "/opt/homebrew/lib/libvulkan.1.dylib"]
    } else {
        &["/usr/lib/x86_64-linux-gnu/libvulkan.so.1", "/usr/lib/aarch64-linux-gnu/libvulkan.so.1",
          "/usr/lib64/libvulkan.so.1", "/usr/lib/libvulkan.so.1"]
    };
    candidates.iter().any(|p| std::path::Path::new(p).exists())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nvidia_smi_rows_are_parsed() {
        let out = "NVIDIA RTX A6000, 49140, 620.70.06\nNVIDIA RTX A6000, 49140, 620.70.06\n";
        let g = parse_nvidia_smi(out);
        assert_eq!(g.len(), 2);
        assert_eq!(g[0], Gpu { vendor: "nvidia".into(), name: "NVIDIA RTX A6000".into(),
                               vram_bytes: Some(49140 * 1024 * 1024), driver: Some("620.70.06".into()) });
        assert!(parse_nvidia_smi("").is_empty());
        assert!(parse_nvidia_smi("No devices were found").is_empty());
    }

    #[test]
    fn apple_silicon_gpu_uses_unified_memory() {
        let json = r#"{"SPDisplaysDataType":[{"sppci_model":"Apple M1 Pro","sppci_cores":"16",
                       "spdisplays_mtlgpufamilysupport":"spdisplays_metal3","sppci_vendor":"sppci_vendor_Apple"}]}"#;
        let g = parse_system_profiler(json);
        assert_eq!(g, vec![Gpu { vendor: "apple".into(), name: "Apple M1 Pro".into(), vram_bytes: None,
                                 driver: Some("metal3".into()) }]);
        let intel_mac = r#"{"SPDisplaysDataType":[{"sppci_model":"AMD Radeon Pro 5500M","spdisplays_vram":"8 GB"}]}"#;
        assert_eq!(parse_system_profiler(intel_mac)[0].vram_bytes, Some(8 << 30));
        assert!(parse_system_profiler("not json").is_empty());
    }

    #[test]
    fn windows_video_controllers_are_parsed() {
        let many = r#"[{"Name":"Intel(R) UHD Graphics","AdapterRAM":2147479552,"DriverVersion":"31.0.101.5186"},
                      {"Name":"NVIDIA RTX 3500 Ada Generation Laptop GPU","AdapterRAM":4293918720,"DriverVersion":"32.0"},
                      {"Name":"Microsoft Basic Display Adapter","AdapterRAM":0,"DriverVersion":"10.0"}]"#;
        let g = parse_cim_video(many);
        assert_eq!(g.iter().map(|x| x.vendor.as_str()).collect::<Vec<_>>(), ["intel", "nvidia"]);
        let one = r#"{"Name":"AMD Radeon RX 7600","AdapterRAM":null,"DriverVersion":"1"}"#;
        assert_eq!(parse_cim_video(one)[0].vram_bytes, None);
    }

    #[test]
    fn linux_virtual_displays_are_not_gpus() {
        assert!(linux_gpu("0x1234", "0x1111", None).is_none()); // QEMU bochs display
        assert_eq!(linux_gpu("0x1002\n", "0x744c\n", Some(24 << 30)).unwrap().vendor, "amd");
    }

    #[test]
    fn backend_detection() {
        let nvidia = Gpu { vendor: "nvidia".into(), name: "x".into(), vram_bytes: Some(1), driver: Some("1".into()) };
        let intel = Gpu { vendor: "intel".into(), name: "y".into(), vram_bytes: None, driver: None };
        let b = backends(&[nvidia.clone(), intel.clone()], true);
        assert_eq!(b.first(), Some(&"cuda"));
        assert!(b.contains(&"vulkan") && b.last() == Some(&"cpu"));
        // An NVIDIA card whose driver did not answer is not a CUDA backend.
        assert!(!backends(&[Gpu { driver: None, ..nvidia }], false).contains(&"cuda"));
        assert_eq!(backends(&[], true), vec!["cpu"]); // a Vulkan loader without a GPU is no use
    }

    #[test]
    fn this_machine_has_a_profile() {
        let h = profile();
        assert!(h.cpu_threads > 0 && h.ram_bytes > 0, "{h:?}");
        assert_eq!(h.backends.last(), Some(&"cpu"));
    }
}
