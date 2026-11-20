//! Records the git commit the agent was built from (IDLEMESH_BUILD_HASH), for crash reports and diagnostic
//! bundles (S4.3, S4.4). "unknown" when built outside a git checkout; "-dirty" marks uncommitted changes.

use std::process::Command;

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn main() {
    let hash = git(&["rev-parse", "--short=12", "HEAD"]).unwrap_or_else(|| "unknown".into());
    let dirty = git(&["status", "--porcelain", "--untracked-files=no", "--", "."]).is_some_and(|s| !s.is_empty());
    println!("cargo:rustc-env=IDLEMESH_BUILD_HASH={hash}{}", if dirty { "-dirty" } else { "" });
    println!("cargo:rerun-if-changed=../.git/HEAD");
    println!("cargo:rerun-if-changed=../.git/index");
}
