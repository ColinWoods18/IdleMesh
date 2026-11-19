"""scripts/idle_timeline.py draws states, sleep and missing data correctly (Sprint 2 demo)."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("idle_timeline", ROOT / "scripts" / "idle_timeline.py")
timeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(timeline)

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_strip_shows_states_sleep_and_gaps():
    samples = [(at(m), "active" if m < 20 else "idle") for m in range(0, 40)]
    spans = timeline.sleep_spans([(at(40), "sleeping"), (at(55), "active")])
    row = timeline.strip(samples, spans, T0, 30, timedelta(minutes=2))
    assert row == "a" * 10 + "I" * 10 + "z" * 8 + "  "


def test_sleep_spans_need_a_wake_event():
    assert timeline.sleep_spans([(at(0), "idle"), (at(5), "sleeping")]) == []
    assert timeline.sleep_spans([(at(5), "sleeping"), (at(9), "idle")]) == [(at(5), at(9))]
