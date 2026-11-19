//! Node states (S2.5): idle, active, sleeping, draining, serving.
//!
//! `classify` decides the state from one moment's inputs; `Tracker` remembers the last state and turns
//! changes into events. Sleep cannot be observed while it happens (the agent is not running), so the
//! tracker spots it afterwards as a gap in the wall clock and records the machine as sleeping for the gap.

use serde::Serialize;
use std::time::{Duration, SystemTime};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum NodeState {
    /// Nobody has used the machine for `idle_after_s`, or the session is locked.
    Idle,
    /// Someone is using the machine, or the agent cannot tell (it never assumes idle).
    Active,
    /// The machine was asleep. Recorded after waking, for the length of the gap.
    Sleeping,
    /// Work is assigned but local policy has paused contribution: the coordinator moves it elsewhere.
    Draining,
    /// Work is assigned and contribution is allowed.
    Serving,
}

impl NodeState {
    pub fn as_str(self) -> &'static str {
        match self {
            NodeState::Idle => "idle",
            NodeState::Active => "active",
            NodeState::Sleeping => "sleeping",
            NodeState::Draining => "draining",
            NodeState::Serving => "serving",
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Inputs {
    pub seconds_since_input: Option<u64>,
    pub session_locked: Option<bool>,
    pub idle_after_s: u64,
    /// The coordinator has assigned this node work (a deployment stage).
    pub assigned: bool,
    /// Local owner policy (pause, battery) currently forbids contribution.
    pub paused: bool,
}

pub fn classify(i: &Inputs) -> NodeState {
    if i.assigned {
        return if i.paused { NodeState::Draining } else { NodeState::Serving };
    }
    let idle_input = i.seconds_since_input.is_some_and(|s| s >= i.idle_after_s);
    if i.session_locked == Some(true) || idle_input {
        NodeState::Idle
    } else {
        NodeState::Active
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Change {
    pub at: SystemTime,
    pub from: Option<NodeState>,
    pub to: NodeState,
}

/// A wall-clock jump longer than this between two checks means the machine was asleep (or the agent was
/// suspended, which looks the same to anyone waiting for it).
pub const SLEEP_GAP: Duration = Duration::from_secs(30);

#[derive(Default)]
pub struct Tracker {
    state: Option<NodeState>,
    last_check: Option<SystemTime>,
}

impl Tracker {
    pub fn state(&self) -> Option<NodeState> {
        self.state
    }

    /// Record the state seen at `now`. Returns the changes since the last call, oldest first; after a
    /// sleep that is a change to `sleeping` at the start of the gap and another at `now`.
    pub fn observe(&mut self, now: SystemTime, next: NodeState) -> Vec<Change> {
        let mut out = Vec::new();
        if let Some(last) = self.last_check {
            if now.duration_since(last).is_ok_and(|gap| gap > SLEEP_GAP) {
                out.push(Change { at: last, from: self.state, to: NodeState::Sleeping });
                self.state = Some(NodeState::Sleeping);
            }
        }
        if self.state != Some(next) {
            out.push(Change { at: now, from: self.state, to: next });
            self.state = Some(next);
        }
        self.last_check = Some(now);
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn inputs(secs: Option<u64>, locked: Option<bool>) -> Inputs {
        Inputs { seconds_since_input: secs, session_locked: locked, idle_after_s: 600, assigned: false,
                 paused: false }
    }

    #[test]
    fn idle_after_threshold_or_when_locked() {
        assert_eq!(classify(&inputs(Some(5), Some(false))), NodeState::Active);
        assert_eq!(classify(&inputs(Some(600), Some(false))), NodeState::Idle);
        assert_eq!(classify(&inputs(Some(5), Some(true))), NodeState::Idle);
    }

    #[test]
    fn unknown_readings_never_count_as_idle() {
        assert_eq!(classify(&inputs(None, None)), NodeState::Active);
    }

    #[test]
    fn assigned_work_is_serving_or_draining() {
        let busy = Inputs { assigned: true, ..inputs(Some(5), Some(false)) };
        assert_eq!(classify(&busy), NodeState::Serving);
        assert_eq!(classify(&Inputs { paused: true, ..busy }), NodeState::Draining);
    }

    #[test]
    fn tracker_reports_only_changes() {
        let t0 = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        let mut t = Tracker::default();
        assert_eq!(t.observe(t0, NodeState::Active),
                   vec![Change { at: t0, from: None, to: NodeState::Active }]);
        assert!(t.observe(t0 + Duration::from_secs(1), NodeState::Active).is_empty());
        let t2 = t0 + Duration::from_secs(2);
        assert_eq!(t.observe(t2, NodeState::Idle),
                   vec![Change { at: t2, from: Some(NodeState::Active), to: NodeState::Idle }]);
    }

    #[test]
    fn a_wall_clock_gap_is_recorded_as_sleep() {
        let t0 = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        let mut t = Tracker::default();
        t.observe(t0, NodeState::Idle);
        let woke = t0 + Duration::from_secs(3600);
        assert_eq!(t.observe(woke, NodeState::Active), vec![
            Change { at: t0, from: Some(NodeState::Idle), to: NodeState::Sleeping },
            Change { at: woke, from: Some(NodeState::Sleeping), to: NodeState::Active },
        ]);
        // Waking into the same state as before still records the sleep.
        let mut t = Tracker::default();
        t.observe(t0, NodeState::Idle);
        assert_eq!(t.observe(woke, NodeState::Idle).len(), 2);
    }
}
