//! Idle monitor (S2.1): how long since the person at this machine last used the keyboard or mouse, whether
//! their session is locked, and how busy the CPU is.
//!
//! Windows reads `GetLastInputInfo` and the session's lock state from the Terminal Services API. Both
//! describe the session the agent runs in, so the agent must run in the user's session: when it becomes a
//! Windows service (S4.1) a small helper in the user session will report these instead. macOS and Linux
//! report nothing yet (S3.1, S3.2), and nothing is guessed: an unknown reading never counts as idle.

use sysinfo::System;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Reading {
    /// Seconds since the last keyboard or mouse input in this session, if the platform can tell.
    pub seconds_since_input: Option<u64>,
    /// Whether the session is locked, if the platform can tell.
    pub session_locked: Option<bool>,
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
        Reading { seconds_since_input: seconds_since_input(), session_locked: session_locked() }
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

#[cfg(not(windows))]
mod platform {
    use super::Reading;

    pub fn read() -> Reading {
        Reading::default() // macOS and Linux: S3.1 and S3.2
    }
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
    }
}
