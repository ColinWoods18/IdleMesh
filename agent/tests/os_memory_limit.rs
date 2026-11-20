//! S4.8: the worker cannot allocate past its cap, even between two watchdog polls. The agent's hidden
//! `limit-test` command starts `alloc-test` under exactly the OS limit the worker gets, and reports the outcome.

use std::process::Command;

fn limit_test(cap_mb: u64, alloc_mb: u64) -> serde_json::Value {
    let out = Command::new(env!("CARGO_BIN_EXE_idlemesh-agent"))
        .args(["limit-test", "--cap-mb", &cap_mb.to_string(), "--alloc-mb", &alloc_mb.to_string()])
        .output().expect("run the agent");
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    serde_json::from_slice(&out.stdout).expect("JSON outcome")
}

#[test]
fn allocations_past_the_cap_are_refused_where_the_os_can_enforce_it() {
    let over = limit_test(64, 512);
    let under = limit_test(512, 64);
    assert_eq!(under["outcome"], "allocated", "{under}");
    if cfg!(any(windows, target_os = "linux")) {
        assert_eq!(over["os_limit"], true, "{over}");
        assert_eq!(over["outcome"], "refused", "{over}");
    } else {
        // macOS enforces no per-process limit: the 250 ms watchdog is the only guard there.
        assert_eq!(over["os_limit"], false, "{over}");
    }
}
