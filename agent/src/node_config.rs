//! Enrollment from a config file (S4.2): groundwork for the installer wizard (S10) and for running as a service
//! (docs/design/agent-service.md), where nobody is at a terminal to type `enroll` options.
//!
//! `idlemesh-agent run --config node.yaml` enrolls on first start (when the state file does not exist yet) using
//! the file's values, then runs. Later starts find the state file and ignore the enrollment fields. The one-time
//! code expires after 10 minutes and works once, so a leftover copy in the file is harmless, but the file is
//! still meant to be readable only by the agent's account.
//!
//! ```yaml
//! coordinator: https://coordinator.example.edu:8765
//! coordinator_fingerprint: ea3742a938ca134a      # at least the first 16 hex digits
//! enrollment_code: enr_....xxxx                  # from `pilot.py code` or the admin page
//! name: Lab PC 12
//! memory_cap_gb: 8
//! allow_battery: false
//! consent:
//!   idle_data: true
//!   diagnostics: true
//! state: state.json                              # relative to this file; this is the default
//! ```

use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ConsentConfig {
    #[serde(default)]
    pub idle_data: bool,
    #[serde(default)]
    pub diagnostics: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct NodeConfig {
    pub coordinator: String,
    pub coordinator_fingerprint: Option<String>,
    pub enrollment_code: Option<String>,
    pub name: String,
    pub memory_cap_gb: f64,
    #[serde(default)]
    pub allow_battery: bool,
    #[serde(default)]
    pub consent: ConsentConfig,
    /// The state file; relative paths are relative to the config file. Default: state.json beside it.
    pub state: Option<PathBuf>,
    /// Inference backend build (rarely needed; the default matches the coordinator's).
    pub backend_digest: Option<String>,
}

impl NodeConfig {
    pub fn load(path: &Path) -> Result<Self, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        let cfg: NodeConfig = serde_yaml::from_str(&text)
            .map_err(|e| format!("{} is not a valid node config: {e}", path.display()))?;
        cfg.validate().map_err(|e| format!("{}: {e}", path.display()))?;
        Ok(cfg)
    }

    fn validate(&self) -> Result<(), String> {
        if !(self.coordinator.starts_with("https://") || self.coordinator.starts_with("http://127.0.0.1")
             || self.coordinator.starts_with("http://localhost")) {
            return Err("coordinator must be an https:// address".into());
        }
        if self.name.trim().is_empty() || self.name.len() > 100 {
            return Err("name must be 1 to 100 characters".into());
        }
        if !(self.memory_cap_gb > 0.0 && self.memory_cap_gb < 100_000.0) {
            return Err("memory_cap_gb must be a positive number of gigabytes".into());
        }
        if let Some(fp) = &self.coordinator_fingerprint {
            let hex: String = fp.chars().filter(|c| !c.is_whitespace() && *c != ':').collect();
            if hex.len() < 16 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
                return Err("coordinator_fingerprint needs at least the first 16 hex digits".into());
            }
        }
        Ok(())
    }

    /// The state file this config names, resolved against the config file's directory.
    pub fn state_path(&self, config_path: &Path) -> PathBuf {
        let dir = config_path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
        match &self.state {
            Some(p) if p.is_absolute() => p.clone(),
            Some(p) => dir.join(p),
            None => dir.join("state.json"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(name: &str, text: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("idlemesh-cfg-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("node.yaml");
        std::fs::write(&p, text).unwrap();
        p
    }

    const GOOD: &str = "coordinator: https://10.0.0.5:8765\ncoordinator_fingerprint: ea3742a938ca134a\n\
                        enrollment_code: enr_x.y\nname: Lab PC 12\nmemory_cap_gb: 8\n\
                        consent:\n  idle_data: true\n  diagnostics: true\n";

    #[test]
    fn a_complete_config_loads_and_resolves_the_state_path() {
        let p = write("good", GOOD);
        let c = NodeConfig::load(&p).unwrap();
        assert_eq!((c.name.as_str(), c.memory_cap_gb, c.consent.diagnostics), ("Lab PC 12", 8.0, true));
        assert!(!c.allow_battery);
        assert_eq!(c.state_path(&p), p.parent().unwrap().join("state.json"));
        let rel = NodeConfig { state: Some("node/s.json".into()), ..c };
        assert_eq!(rel.state_path(&p), p.parent().unwrap().join("node/s.json"));
    }

    #[test]
    fn mistakes_fail_with_the_problem_named() {
        for (text, problem) in [
            (GOOD.replace("https://", "http://"), "https"),
            (GOOD.replace("memory_cap_gb: 8", "memory_cap_gb: 0"), "memory_cap_gb"),
            (GOOD.replace("ea3742a938ca134a", "xyz"), "fingerprint"),
            (format!("{GOOD}memory_cap: 4\n"), "unknown field"),
            (GOOD.replace("name: Lab PC 12\n", ""), "name"),
        ] {
            let err = NodeConfig::load(&write("bad", &text)).unwrap_err();
            assert!(err.contains(problem), "{problem}: {err}");
        }
    }
}
