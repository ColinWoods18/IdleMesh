//! Idle monitor (S2.1, S3.1, S3.2): how long since the person at this machine last used the keyboard or
//! mouse, whether their session is locked, whether a laptop lid is closed, and how busy the CPU is.
//!
//! - **Windows** reads `GetLastInputInfo` and the session's lock state from the Terminal Services API. Both
//!   describe the session the agent runs in, so the agent must run in the user's session (see
//!   docs/design/agent-service.md for how a service will get these later).
//! - **macOS** reads IOKit's `HIDIdleTime` (system-wide, any user), the console session's lock state from
//!   CoreGraphics, and the lid (`AppleClamshellState`) from the power-management root domain.
//! - **Linux** asks logind about a local graphical session (`IdleHint`, `IdleSinceHint`, `LockedHint`).
//!   Headless machines have none, so they fall back to terminal activity: the last input on any login
//!   terminal (what `w` shows as IDLE), and when nobody is logged in, the time since the last login or
//!   logout recorded in wtmp (or since boot).
//!
//! Nothing is guessed: an unknown reading never counts as idle (D-17).

use sysinfo::System;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Reading {
    /// Seconds since the last keyboard or mouse input in this session, if the platform can tell.
    pub seconds_since_input: Option<u64>,
    /// Whether the session is locked, if the platform can tell.
    pub session_locked: Option<bool>,
    /// Whether a laptop lid is closed (macOS only for now; None elsewhere or on desktops).
    pub lid_closed: Option<bool>,
    /// Where the reading came from, for logs and the hardware profile.
    pub source: &'static str,
}

pub fn read() -> Reading {
    platform::read()
}

/// CPU use across all cores, 0.0 to 1.0, averaged since the previous call.
pub struct CpuSampler {
    sys: System,
}

impl CpuSampler {
    pub fn new() -> Self {
        let mut sys = System::new();
        sys.refresh_cpu_usage(); // the first figure needs a baseline
        CpuSampler { sys }
    }

    pub fn sample(&mut self) -> f64 {
        self.sys.refresh_cpu_usage();
        let pct = f64::from(self.sys.global_cpu_usage());
        ((pct / 100.0).clamp(0.0, 1.0) * 1000.0).round() / 1000.0
    }
}

#[cfg(windows)]
mod platform {
    use super::Reading;
    use windows_sys::Win32::System::RemoteDesktop::{
        WTSFreeMemory, WTSQuerySessionInformationW, WTSSessionInfoEx, WTSINFOEXW, WTS_CURRENT_SERVER_HANDLE,
    };
    use windows_sys::Win32::System::SystemInformation::GetTickCount;
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};

    const WTS_CURRENT_SESSION: u32 = u32::MAX;
    const WTS_SESSIONSTATE_LOCK: i32 = 0;
    const WTS_SESSIONSTATE_UNLOCK: i32 = 1;

    pub fn read() -> Reading {
        Reading { seconds_since_input: seconds_since_input(), session_locked: session_locked(), lid_closed: None,
                  source: "win32" }
    }

    fn seconds_since_input() -> Option<u64> {
        let mut info = LASTINPUTINFO { cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32, dwTime: 0 };
        // SAFETY: `info` is a valid LASTINPUTINFO with cbSize set, as the API requires.
        if unsafe { GetLastInputInfo(&mut info) } == 0 {
            return None;
        }
        // Both are 32-bit millisecond tick counts; wrapping_sub stays correct across the 49.7-day wrap.
        // SAFETY: GetTickCount has no preconditions.
        let now = unsafe { GetTickCount() };
        Some(u64::from(now.wrapping_sub(info.dwTime)) / 1000)
    }

    fn session_locked() -> Option<bool> {
        let mut buf: *mut u16 = std::ptr::null_mut();
        let mut len = 0u32;
        // SAFETY: on success the API allocates `buf` (freed below with WTSFreeMemory) and sets `len`.
        let ok = unsafe {
            WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSSessionInfoEx, &mut buf,
                                        &mut len)
        };
        if ok == 0 || buf.is_null() {
            return None;
        }
        let flags = if len as usize >= std::mem::size_of::<WTSINFOEXW>() {
            // SAFETY: the buffer holds a WTSINFOEXW (checked by size); level 1 is the only defined level.
            let info = unsafe { &*(buf as *const WTSINFOEXW) };
            (info.Level == 1).then_some(unsafe { info.Data.WTSInfoExLevel1.SessionFlags })
        } else {
            None
        };
        // SAFETY: `buf` came from WTSQuerySessionInformationW.
        unsafe { WTSFreeMemory(buf.cast()) };
        match flags {
            Some(WTS_SESSIONSTATE_LOCK) => Some(true),
            Some(WTS_SESSIONSTATE_UNLOCK) => Some(false),
            _ => None,
        }
    }
}

#[cfg(target_os = "macos")]
mod platform {
    //! IOKit, CoreFoundation and CoreGraphics through their C APIs: no helper processes, so reading every
    //! second costs microseconds. CF objects follow the Create/Copy rule: whatever we create, we release.
    use super::Reading;
    use std::ffi::{c_char, c_void, CString};

    type CFTypeRef = *const c_void;
    type CFStringRef = *const c_void;
    type CFDictionaryRef = *const c_void;
    type IoObject = u32;

    const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
    const K_CF_NUMBER_SINT64_TYPE: isize = 4;
    const K_IO_MAIN_PORT_DEFAULT: u32 = 0;

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringCreateWithCString(alloc: *const c_void, s: *const c_char, encoding: u32) -> CFStringRef;
        fn CFRelease(cf: CFTypeRef);
        fn CFGetTypeID(cf: CFTypeRef) -> usize;
        fn CFNumberGetTypeID() -> usize;
        fn CFBooleanGetTypeID() -> usize;
        fn CFNumberGetValue(number: CFTypeRef, the_type: isize, value: *mut c_void) -> bool;
        fn CFBooleanGetValue(boolean: CFTypeRef) -> bool;
        fn CFDictionaryGetValue(dict: CFDictionaryRef, key: *const c_void) -> *const c_void;
    }

    #[link(name = "IOKit", kind = "framework")]
    extern "C" {
        fn IOServiceMatching(name: *const c_char) -> *const c_void;
        fn IOServiceNameMatching(name: *const c_char) -> *const c_void;
        fn IOServiceGetMatchingService(main_port: u32, matching: *const c_void) -> IoObject;
        fn IORegistryEntryCreateCFProperty(entry: IoObject, key: CFStringRef, alloc: *const c_void, options: u32)
                                           -> CFTypeRef;
        fn IOObjectRelease(obj: IoObject) -> i32;
    }

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGSessionCopyCurrentDictionary() -> CFDictionaryRef;
    }

    /// A CFString we own.
    struct CfStr(CFStringRef);

    impl CfStr {
        fn new(s: &str) -> Option<Self> {
            let c = CString::new(s).ok()?;
            // SAFETY: `c` is a valid NUL-terminated string; a null result is handled.
            let r = unsafe { CFStringCreateWithCString(std::ptr::null(), c.as_ptr(), K_CF_STRING_ENCODING_UTF8) };
            (!r.is_null()).then_some(CfStr(r))
        }
    }

    impl Drop for CfStr {
        fn drop(&mut self) {
            // SAFETY: created above with a Create function, released once.
            unsafe { CFRelease(self.0) }
        }
    }

    pub fn read() -> Reading {
        Reading { seconds_since_input: hid_idle_seconds(), session_locked: session_locked(), lid_closed: lid_closed(),
                  source: "iokit" }
    }

    /// A property of the first IOKit service matching `class` (or, with `by_name`, named `class`).
    fn service_property(class: &str, by_name: bool, key: &str) -> Option<CFTypeRef> {
        let c = CString::new(class).ok()?;
        let key = CfStr::new(key)?;
        // SAFETY: the matching dictionary is consumed by IOServiceGetMatchingService; the service is released.
        unsafe {
            let matching = if by_name { IOServiceNameMatching(c.as_ptr()) } else { IOServiceMatching(c.as_ptr()) };
            if matching.is_null() {
                return None;
            }
            let service = IOServiceGetMatchingService(K_IO_MAIN_PORT_DEFAULT, matching);
            if service == 0 {
                return None;
            }
            let value = IORegistryEntryCreateCFProperty(service, key.0, std::ptr::null(), 0);
            IOObjectRelease(service);
            (!value.is_null()).then_some(value)
        }
    }

    /// Nanoseconds since the last HID event, as seconds. System-wide, so it works from any session.
    fn hid_idle_seconds() -> Option<u64> {
        let v = service_property("IOHIDSystem", false, "HIDIdleTime")?;
        let mut ns: i64 = 0;
        // SAFETY: `v` is a CF object we own (Create rule); its type is checked before reading it as a number.
        let ok = unsafe {
            let ok = CFGetTypeID(v) == CFNumberGetTypeID()
                && CFNumberGetValue(v, K_CF_NUMBER_SINT64_TYPE, (&mut ns as *mut i64).cast());
            CFRelease(v);
            ok
        };
        (ok && ns >= 0).then_some(ns as u64 / 1_000_000_000)
    }

    fn lid_closed() -> Option<bool> {
        let v = service_property("IOPMrootDomain", true, "AppleClamshellState")?;
        // SAFETY: as above; a Mac without a lid has no such property and returned None already.
        unsafe {
            let out = (CFGetTypeID(v) == CFBooleanGetTypeID()).then(|| CFBooleanGetValue(v));
            CFRelease(v);
            out
        }
    }

    /// The console session's lock state. Locked when the screen is locked, or when the session is not on the
    /// console (fast user switching, or the login window). None when there is no window-server session at all
    /// (the agent runs as a daemon): then only HIDIdleTime is known.
    fn session_locked() -> Option<bool> {
        // SAFETY: Copy rule: the dictionary is released below; values from CFDictionaryGetValue are borrowed.
        unsafe {
            let dict = CGSessionCopyCurrentDictionary();
            if dict.is_null() {
                return None;
            }
            let flag = |name: &str| -> Option<bool> {
                let key = CfStr::new(name)?;
                let v = CFDictionaryGetValue(dict, key.0);
                (!v.is_null() && CFGetTypeID(v) == CFBooleanGetTypeID()).then(|| CFBooleanGetValue(v))
            };
            let locked = flag("CGSSessionScreenIsLocked").unwrap_or(false);
            let on_console = flag("kCGSSessionOnConsoleKey").unwrap_or(true);
            CFRelease(dict);
            Some(locked || !on_console)
        }
    }
}

#[cfg(target_os = "linux")]
mod platform {
    use super::Reading;
    use std::path::Path;
    use std::process::Command;
    use std::sync::Mutex;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    /// logind is asked which sessions exist at most this often; a graphical session's hints every second.
    const SESSION_SCAN: Duration = Duration::from_secs(10);

    struct Cache {
        scanned: Option<Instant>,
        graphical: Option<String>,
    }

    static CACHE: Mutex<Cache> = Mutex::new(Cache { scanned: None, graphical: None });

    pub fn read() -> Reading {
        let now = SystemTime::now();
        let session = {
            let mut c = CACHE.lock().unwrap_or_else(|e| e.into_inner());
            if c.scanned.is_none_or(|t| t.elapsed() >= SESSION_SCAN) {
                c.graphical = graphical_session();
                c.scanned = Some(Instant::now());
            }
            c.graphical.clone()
        };
        if let Some(id) = session {
            if let Some(props) = loginctl(&["show-session", &id, "-p", "IdleHint", "-p", "IdleSinceHint",
                                            "-p", "LockedHint", "-p", "Active"]) {
                if let Some(r) = super::logind_reading(&props, now) {
                    return r;
                }
            }
            CACHE.lock().unwrap_or_else(|e| e.into_inner()).scanned = None; // the session went away: rescan
        }
        let terminals = terminal_atimes();
        let last_session_change = std::fs::metadata("/var/log/wtmp").and_then(|m| m.modified()).ok()
            .into_iter().chain(boot_time()).max();
        Reading { seconds_since_input: super::headless_idle_seconds(&terminals, last_session_change, now),
                  session_locked: None, lid_closed: None, source: "tty" }
    }

    fn loginctl(args: &[&str]) -> Option<String> {
        let out = Command::new("loginctl").args(args).output().ok()?;
        out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
    }

    /// The active, local, graphical user session, if there is one.
    fn graphical_session() -> Option<String> {
        let list = loginctl(&["list-sessions", "--no-legend"])?;
        for id in list.lines().filter_map(|l| l.split_whitespace().next()) {
            let props = loginctl(&["show-session", id, "-p", "Type", "-p", "Remote", "-p", "Class", "-p", "Active"])?;
            if super::is_local_graphical(&props) {
                return Some(id.to_string());
            }
        }
        None
    }

    /// Last access time of every login terminal in use: /dev/pts/N (SSH, terminal windows, tmux panes) and
    /// /dev/ttyN consoles that someone is logged in to (owned by a user other than root).
    fn terminal_atimes() -> Vec<SystemTime> {
        use std::os::unix::fs::MetadataExt;
        let mut out = Vec::new();
        let scan = |dir: &str, prefix: &str, need_user: bool, out: &mut Vec<SystemTime>| {
            let Ok(rd) = std::fs::read_dir(dir) else { return };
            for e in rd.flatten() {
                let name = e.file_name();
                let Some(rest) = name.to_str().and_then(|n| n.strip_prefix(prefix)) else { continue };
                if rest.is_empty() || !rest.bytes().all(|b| b.is_ascii_digit()) {
                    continue;
                }
                if let Ok(m) = std::fs::metadata(e.path()) {
                    if need_user && m.uid() == 0 {
                        continue;
                    }
                    out.push(UNIX_EPOCH + Duration::from_secs(m.atime().max(0) as u64));
                }
            }
        };
        scan("/dev/pts", "", false, &mut out);
        scan("/dev", "tty", true, &mut out);
        out
    }

    fn boot_time() -> Option<SystemTime> {
        let up: f64 = std::fs::read_to_string(Path::new("/proc/uptime")).ok()?.split_whitespace().next()?
            .parse().ok()?;
        SystemTime::now().checked_sub(Duration::from_secs_f64(up))
    }
}

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
mod platform {
    use super::Reading;

    pub fn read() -> Reading {
        Reading { source: "none", ..Reading::default() }
    }
}

/// `loginctl show-session -p ...` output as key=value pairs.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn props(text: &str) -> std::collections::HashMap<&str, &str> {
    text.lines().filter_map(|l| l.split_once('=')).collect()
}

/// A session logind can report keyboard and mouse idleness for: graphical, local, a user's, and active.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn is_local_graphical(text: &str) -> bool {
    let p = props(text);
    matches!(p.get("Type"), Some(&("x11" | "wayland" | "mir"))) && p.get("Remote") == Some(&"no")
        && p.get("Class") == Some(&"user") && p.get("Active") == Some(&"yes")
}

/// Idle from a graphical session's hints. Desktops set IdleHint only after their own idle delay (GNOME: 5
/// minutes), so before that the exact time is unknown and reported as 0: active, never guessed idle.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn logind_reading(text: &str, now: std::time::SystemTime) -> Option<Reading> {
    let p = props(text);
    if p.get("Active") != Some(&"yes") {
        return None;
    }
    let idle = p.get("IdleHint") == Some(&"yes");
    let since_us: Option<u64> = p.get("IdleSinceHint").and_then(|v| v.parse().ok()).filter(|v| *v > 0);
    let now_us = now.duration_since(std::time::UNIX_EPOCH).ok()?.as_micros() as u64;
    let seconds = if idle { since_us.map(|s| now_us.saturating_sub(s) / 1_000_000) } else { Some(0) };
    Some(Reading { seconds_since_input: seconds, session_locked: p.get("LockedHint").map(|v| *v == "yes"),
                   lid_closed: None, source: "logind" })
}

/// Headless fallback: seconds since the most recent of: input on any terminal in use, and the last login or
/// logout (or boot). A logout a minute ago counts even if another user has an old idle terminal open.
/// Unknown only when none of these can be read.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn headless_idle_seconds(terminals: &[std::time::SystemTime], last_session_change: Option<std::time::SystemTime>,
                         now: std::time::SystemTime) -> Option<u64> {
    let latest = terminals.iter().copied().chain(last_session_change).max()?;
    Some(now.duration_since(latest).map_or(0, |d| d.as_secs()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpu_use_is_a_fraction() {
        let mut s = CpuSampler::new();
        std::thread::sleep(std::time::Duration::from_millis(250));
        let v = s.sample();
        assert!((0.0..=1.0).contains(&v), "{v}");
    }

    #[cfg(windows)]
    #[test]
    fn windows_reports_idle_time() {
        // Every process runs in some session, so the input clock is readable. The lock state can be unknown
        // in a non-interactive session (a CI runner), so it is not asserted here.
        let r = read();
        assert!(r.seconds_since_input.is_some());
        assert_eq!(r.source, "win32");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_reports_hid_idle_time() {
        // HIDIdleTime is system-wide, so even a CI runner without a console session can read it.
        let r = read();
        assert!(r.seconds_since_input.is_some(), "{r:?}");
        assert_eq!(r.source, "iokit");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_reports_something_or_says_unknown() {
        let r = read();
        assert!(r.source == "logind" || r.source == "tty", "{r:?}");
        // A CI runner has no terminals but has /proc/uptime, so the headless path still knows something.
        if r.source == "tty" {
            assert!(r.seconds_since_input.is_some(), "{r:?}");
        }
    }

    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    fn at(s: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(s)
    }

    #[test]
    fn graphical_sessions_are_recognised() {
        assert!(is_local_graphical("Type=wayland\nRemote=no\nClass=user\nActive=yes\n"));
        assert!(is_local_graphical("Type=x11\nRemote=no\nClass=user\nActive=yes"));
        assert!(!is_local_graphical("Type=tty\nRemote=yes\nClass=user\nActive=yes")); // SSH
        assert!(!is_local_graphical("Type=wayland\nRemote=no\nClass=greeter\nActive=yes")); // login screen
        assert!(!is_local_graphical("Type=x11\nRemote=no\nClass=user\nActive=no")); // switched away
    }

    #[test]
    fn logind_hints_become_a_reading() {
        let now = at(1_000_000);
        let idle = "IdleHint=yes\nIdleSinceHint=999400000000\nLockedHint=no\nActive=yes";
        let r = logind_reading(idle, now).unwrap();
        assert_eq!((r.seconds_since_input, r.session_locked, r.source), (Some(600), Some(false), "logind"));
        // Before the desktop's own idle delay passes, the exact time is unknown: report active.
        let busy = "IdleHint=no\nIdleSinceHint=0\nLockedHint=yes\nActive=yes";
        let r = logind_reading(busy, now).unwrap();
        assert_eq!((r.seconds_since_input, r.session_locked), (Some(0), Some(true)));
        assert!(logind_reading("IdleHint=yes\nActive=no", now).is_none());
        // Idle without a timestamp is unknown, not idle.
        assert_eq!(logind_reading("IdleHint=yes\nIdleSinceHint=0\nActive=yes", now).unwrap().seconds_since_input,
                   None);
    }

    #[test]
    fn headless_idle_uses_the_most_recent_activity() {
        let now = at(10_000);
        // Two terminals: the most recently used one counts.
        assert_eq!(headless_idle_seconds(&[at(9_000), at(9_900)], Some(at(1_000)), now), Some(100));
        // Nobody logged in: time since the last logout.
        assert_eq!(headless_idle_seconds(&[], Some(at(7_000)), now), Some(3_000));
        // A recent logout counts even with an old idle terminal still open.
        assert_eq!(headless_idle_seconds(&[at(1_000)], Some(at(9_000)), now), Some(1_000));
        // A clock that went backwards reads as active, not as a huge idle time.
        assert_eq!(headless_idle_seconds(&[at(20_000)], None, now), Some(0));
        assert_eq!(headless_idle_seconds(&[], None, now), None);
    }
}
