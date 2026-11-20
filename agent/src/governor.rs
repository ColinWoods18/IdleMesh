//! Contribution governor v0 (S4.9, D-12): how much of this machine IdleMesh may use right now, as a level from
//! 0.0 to 1.0 with the reason for it.
//!
//! - **Owner pause or battery policy:** 0.0 (`policy` or `power`).
//! - **Someone is using the machine** (input within `idle_after_s`, or unknown and not locked): the site's
//!   `yield_level`, about 2% (`user_activity`). Checked every second, so the level drops within ~1 s of input.
//! - **Idle:** ramps from `yield_level` to 1.0 over `ramp_s` after `idle_after_s` (`idle_ramp`).
//! - **Ceilings:** the time window in force now (local time, from the site config: `time_window`) and the owner's
//!   `max_level` (`policy`) cap the result.
//!
//! The level is applied to the worker through CPU priority (immediately), its thread count (when it next
//! starts) and the memory cap (the ceiling, not the momentary yield: memory is freed at handoff, not on yield, D-12).

use chrono::{Datelike, Timelike};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct Window {
    #[serde(default)]
    pub name: String,
    pub days: Vec<String>,
    pub start: String,
    pub end: String,
    pub max_level: f64,
}

/// The site's governor settings, sent by the coordinator with each heartbeat reply.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct Settings {
    pub idle_after_s: u64,
    pub ramp_s: u64,
    pub yield_level: f64,
    #[serde(default = "one")]
    pub max_cpu_fraction: f64,
    #[serde(default)]
    pub windows: Vec<Window>,
}

fn one() -> f64 {
    1.0
}

impl Default for Settings {
    fn default() -> Self {
        Settings { idle_after_s: 600, ramp_s: 1800, yield_level: 0.02, max_cpu_fraction: 0.75, windows: Vec::new() }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Reason {
    IdleRamp,
    TimeWindow,
    Policy,
    UserActivity,
    Power,
}

impl Reason {
    pub fn as_str(self) -> &'static str {
        match self {
            Reason::IdleRamp => "idle_ramp",
            Reason::TimeWindow => "time_window",
            Reason::Policy => "policy",
            Reason::UserActivity => "user_activity",
            Reason::Power => "power",
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Inputs {
    pub seconds_since_input: Option<u64>,
    pub session_locked: Option<bool>,
    /// The owner paused contribution.
    pub owner_paused: bool,
    /// Contribution is paused by battery or unknown-power policy.
    pub power_paused: bool,
    /// The owner's ceiling (0.0 to 1.0).
    pub max_level: f64,
    /// Local weekday (0 = Monday) and minute of the day.
    pub weekday: u32,
    pub minute: u32,
}

impl Inputs {
    /// Local day and time from the clock.
    pub fn now_local() -> (u32, u32) {
        let t = chrono::Local::now();
        (t.weekday().num_days_from_monday(), t.hour() * 60 + t.minute())
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Level {
    pub level: f64,
    pub reason: Reason,
    /// The level without the momentary user-activity yield: what memory and thread counts follow.
    pub ceiling: f64,
}

const DAYS: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

fn minutes(hhmm: &str) -> Option<u32> {
    let (h, m) = hhmm.split_once(':')?;
    Some(h.parse::<u32>().ok()? * 60 + m.parse::<u32>().ok()?)
}

/// The time window in force: one that names this day and covers this minute. A window that ends before it starts
/// (20:00 to 07:00) runs past midnight, and its early-morning part belongs to the day it started.
pub fn window_ceiling(windows: &[Window], weekday: u32, minute: u32) -> Option<f64> {
    let today = DAYS[weekday as usize % 7];
    let yesterday = DAYS[(weekday as usize + 6) % 7];
    windows.iter().filter_map(|w| {
        let (s, e) = (minutes(&w.start)?, minutes(&w.end)?);
        let has = |d: &str| w.days.iter().any(|x| x == d);
        let inside = if s <= e {
            has(today) && minute >= s && minute < e
        } else {
            (has(today) && minute >= s) || (has(yesterday) && minute < e)
        };
        inside.then_some(w.max_level)
    }).reduce(f64::min)
}

pub fn decide(s: &Settings, i: &Inputs) -> Level {
    let round = |v: f64| (v.clamp(0.0, 1.0) * 100.0).round() / 100.0;
    if i.owner_paused {
        return Level { level: 0.0, reason: Reason::Policy, ceiling: 0.0 };
    }
    if i.power_paused {
        return Level { level: 0.0, reason: Reason::Power, ceiling: 0.0 };
    }
    // Ceilings: the time window now, then the owner's own limit.
    let mut ceiling = 1.0;
    let mut ceiling_reason = Reason::IdleRamp;
    if let Some(w) = window_ceiling(&s.windows, i.weekday, i.minute) {
        if w < ceiling {
            ceiling = w;
            ceiling_reason = Reason::TimeWindow;
        }
    }
    if i.max_level < ceiling {
        ceiling = i.max_level;
        ceiling_reason = Reason::Policy;
    }
    let locked = i.session_locked == Some(true);
    let idle_for = match (i.seconds_since_input, locked) {
        (Some(secs), _) if secs >= s.idle_after_s => Some(secs - s.idle_after_s),
        (_, true) => Some(0), // a locked session counts as idle from now (D-17), ramping from the start
        _ => None,            // input recently, or unknown: never assumed idle
    };
    let Some(idle_for) = idle_for else {
        return Level { level: round(s.yield_level.min(ceiling)), reason: Reason::UserActivity, ceiling: round(ceiling) };
    };
    let ramp = if s.ramp_s == 0 { 1.0 } else { (idle_for as f64 / s.ramp_s as f64).min(1.0) };
    let wanted = s.yield_level + (1.0 - s.yield_level) * ramp;
    if wanted > ceiling {
        Level { level: round(ceiling), reason: ceiling_reason, ceiling: round(ceiling) }
    } else {
        Level { level: round(wanted), reason: Reason::IdleRamp, ceiling: round(ceiling) }
    }
}

/// Tracks the level and says when a change is worth logging: the reason changed, or the level moved by 0.05 or
/// more since the last logged value (a ramp would otherwise log every second).
#[derive(Default)]
pub struct Governor {
    pub current: Option<Level>,
    logged: Option<Level>,
}

impl Governor {
    pub fn update(&mut self, next: Level) -> Option<(Option<Level>, Level)> {
        self.current = Some(next);
        let changed = match self.logged {
            None => true,
            Some(prev) => prev.reason != next.reason || (prev.level - next.level).abs() >= 0.05,
        };
        if changed {
            let prev = self.logged.replace(next);
            return Some((prev, next));
        }
        None
    }
}

/// Worker threads for a level: at least one, at most the configured count times the site's CPU fraction.
pub fn threads(max_threads: u32, max_cpu_fraction: f64, level: f64) -> u32 {
    ((max_threads as f64 * max_cpu_fraction.clamp(0.0, 1.0) * level).round() as u32).clamp(1, max_threads.max(1))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings() -> Settings {
        Settings { idle_after_s: 600, ramp_s: 1800, yield_level: 0.02, max_cpu_fraction: 0.75, windows: vec![
            Window { name: "overnight".into(), days: DAYS.iter().map(|d| d.to_string()).collect(),
                     start: "20:00".into(), end: "07:00".into(), max_level: 1.0 },
            Window { name: "daytime".into(), days: ["mon", "tue", "wed", "thu", "fri"].map(String::from).to_vec(),
                     start: "07:00".into(), end: "20:00".into(), max_level: 0.5 },
        ] }
    }

    fn inputs(secs: Option<u64>, weekday: u32, hhmm: &str) -> Inputs {
        Inputs { seconds_since_input: secs, session_locked: Some(false), owner_paused: false, power_paused: false,
                 max_level: 1.0, weekday, minute: minutes(hhmm).unwrap() }
    }

    #[test]
    fn user_activity_yields_at_once() {
        let l = decide(&settings(), &inputs(Some(3), 5, "23:00"));
        assert_eq!((l.level, l.reason), (0.02, Reason::UserActivity));
        assert_eq!(l.ceiling, 1.0); // memory is not given up on a yield
        // Unknown input is not idle either.
        assert_eq!(decide(&settings(), &inputs(None, 5, "23:00")).reason, Reason::UserActivity);
    }

    #[test]
    fn idle_ramps_up_to_the_ceiling() {
        let s = settings();
        let at = |secs| decide(&s, &inputs(Some(secs), 5, "23:00")); // Saturday night: ceiling 1.0
        assert_eq!(at(600).level, 0.02);
        assert_eq!(at(600 + 900).level, 0.51);
        assert_eq!((at(600 + 1800).level, at(600 + 1800).reason), (1.0, Reason::IdleRamp));
        assert_eq!(at(600 + 99_999).level, 1.0);
    }

    #[test]
    fn time_windows_cap_the_level() {
        let s = settings();
        let weekday_noon = decide(&s, &inputs(Some(10_000), 2, "12:00"));
        assert_eq!((weekday_noon.level, weekday_noon.reason), (0.5, Reason::TimeWindow));
        let saturday_noon = decide(&s, &inputs(Some(10_000), 5, "12:00")); // no window covers it: no cap
        assert_eq!(saturday_noon.level, 1.0);
        // Overnight runs past midnight: Tuesday 03:00 belongs to Monday's window.
        assert_eq!(window_ceiling(&s.windows, 1, minutes("03:00").unwrap()), Some(1.0));
        assert_eq!(window_ceiling(&s.windows, 1, minutes("07:00").unwrap()), Some(0.5));
    }

    #[test]
    fn owner_settings_and_power_win() {
        let s = settings();
        let capped = decide(&s, &Inputs { max_level: 0.3, ..inputs(Some(10_000), 5, "23:00") });
        assert_eq!((capped.level, capped.reason), (0.3, Reason::Policy));
        let paused = decide(&s, &Inputs { owner_paused: true, ..inputs(Some(10_000), 5, "23:00") });
        assert_eq!((paused.level, paused.reason), (0.0, Reason::Policy));
        let battery = decide(&s, &Inputs { power_paused: true, ..inputs(Some(10_000), 5, "23:00") });
        assert_eq!((battery.level, battery.reason), (0.0, Reason::Power));
    }

    #[test]
    fn a_locked_session_starts_the_ramp() {
        let l = decide(&settings(), &Inputs { session_locked: Some(true), ..inputs(Some(5), 5, "23:00") });
        assert_eq!((l.level, l.reason), (0.02, Reason::IdleRamp));
    }

    #[test]
    fn only_meaningful_changes_are_logged() {
        let s = settings();
        let mut g = Governor::default();
        assert!(g.update(decide(&s, &inputs(Some(700), 5, "23:00"))).is_some()); // first value
        assert!(g.update(decide(&s, &inputs(Some(710), 5, "23:00"))).is_none()); // ramp moved a little
        assert!(g.update(decide(&s, &inputs(Some(900), 5, "23:00"))).is_some()); // moved >= 0.05
        let (prev, next) = g.update(decide(&s, &inputs(Some(1), 5, "23:00"))).unwrap(); // user came back
        assert_eq!((prev.unwrap().reason, next.reason), (Reason::IdleRamp, Reason::UserActivity));
    }

    #[test]
    fn thread_counts_follow_the_level() {
        assert_eq!(threads(8, 0.75, 1.0), 6);
        assert_eq!(threads(8, 0.75, 0.02), 1);
        assert_eq!(threads(1, 1.0, 0.0), 1);
    }

    #[test]
    fn settings_parse_from_a_heartbeat_reply() {
        let v = serde_json::json!({"idle_after_s": 60, "ramp_s": 120, "yield_level": 0.05, "max_cpu_fraction": 0.5,
                                   "windows": [{"name": "w", "days": ["mon"], "start": "07:00", "end": "20:00",
                                                "max_level": 0.5}]});
        let s: Settings = serde_json::from_value(v).unwrap();
        assert_eq!((s.idle_after_s, s.windows.len()), (60, 1));
    }
}
