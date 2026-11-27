"""Availability predictor v0 (S6.1): runs, hour-of-week counts, shrinkage, the placement term."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from idlemesh_coord.predictor import model
from idlemesh_coord.predictor.evaluate import outcomes
from idlemesh_coord.predictor.model import Counts, Sample

UTC = timezone.utc
MON = datetime(2026, 11, 23, tzinfo=UTC)  # a Monday, 00:00


def series(start, states, step_s=60):
    return [Sample(start + timedelta(seconds=i * step_s), s) for i, s in enumerate(states)]


def test_hour_of_week_starts_monday_midnight_in_the_given_zone():
    assert model.hour_of_week(MON, UTC) == 0
    assert model.hour_of_week(MON + timedelta(days=6, hours=23, minutes=59), UTC) == 167
    plus2 = timezone(timedelta(hours=2))
    assert model.hour_of_week(MON, plus2) == 2   # 02:00 Monday there
    assert model.hour_of_week(MON - timedelta(hours=1), UTC) == 167  # Sunday 23:00


def test_runs_end_at_an_unavailable_sample_a_gap_or_the_end_of_the_data():
    s = series(MON, [True, True, False, True, True])
    assert model.runs(s, 60, 180) == [(MON, MON + timedelta(minutes=2), False),
                                      (MON + timedelta(minutes=3), MON + timedelta(minutes=5), True)]
    gap = [Sample(MON, True), Sample(MON + timedelta(minutes=1), True), Sample(MON + timedelta(minutes=30), True)]
    assert model.runs(gap, 60, 180) == [(MON, MON + timedelta(minutes=2), False),
                                        (MON + timedelta(minutes=30), MON + timedelta(minutes=31), True)]


def test_staying_idle_is_counted_per_hour_and_horizon():
    # Idle for 90 minutes from 09:00 Monday, then active for 30.
    s = series(MON + timedelta(hours=9), [True] * 90 + [False] * 30)
    c = model.count(s, [30, 60], 60, 180, UTC)
    n30, k30 = c.horizon(30)
    n60, k60 = c.horizon(60)
    assert c.seen[9] == 60 and c.avail[9] == 60 and c.seen[10] == 60 and c.avail[10] == 30
    # At 09:mm the run (ending 10:30) lasts another 30 minutes if mm <= 60; for 60 minutes if mm <= 30.
    assert (n30[9], k30[9]) == (60, 60)
    assert (n60[9], k60[9]) == (60, 31)
    assert (n30[10], k30[10]) == (30, 1)


def test_censored_samples_are_left_out():
    s = series(MON, [True] * 20)  # the data ends while still idle
    n, k = model.count(s, [30], 60, 180, UTC).horizon(30)
    assert (n[0], k[0]) == (0, 0)  # nobody knows whether these stayed idle for 30 minutes


def brute_force(samples, horizons, interval_s, max_gap_s, tz):
    c = Counts()
    for m in horizons:
        c.horizon(m)
    runs = model.runs(samples, interval_s, max_gap_s)
    for s in samples:
        h = model.hour_of_week(s.ts, tz)
        c.seen[h] += 1
        c.avail[h] += s.available
        run = next((r for r in runs if r[0] <= s.ts < r[1]), None)
        if not s.available or run is None:
            continue
        for m in horizons:
            stays = s.ts + timedelta(minutes=m) <= run[1]
            if run[2] and not stays:
                continue
            n, k = c.horizon(m)
            n[h] += 1
            k[h] += stays
    return c


def test_one_pass_count_matches_the_definition():
    rng = random.Random(7)
    t, out, state = MON, [], True
    for _ in range(3000):
        t += timedelta(seconds=60 if rng.random() > 0.01 else rng.choice([600, 3600]))
        state = state if rng.random() > 0.05 else not state
        out.append(Sample(t, state))
    fast, ref = model.count(out, [30, 60], 60, 180, UTC), brute_force(out, [30, 60], 60, 180, UTC)
    assert (fast.seen, fast.avail, fast.n, fast.stayed) == (ref.seen, ref.avail, ref.n, ref.stayed)


def test_an_hour_with_few_samples_leans_on_the_node_and_the_fleet():
    c = Counts()
    n, k = c.horizon(60)
    n[9], k[9] = 100, 90  # 09:00 Monday: stays idle 90% of the time
    n[10], k[10] = 1, 0
    fleet = (0.5, 0.5)
    e9 = model.estimate(c, 9, 60, fleet, 10)
    e10 = model.estimate(c, 10, 60, fleet, 10)
    assert e9.basis == "hour" and e9.p_stay == pytest.approx(0.88, abs=0.02)
    assert 0.7 < e10.p_stay < e9.p_stay  # one failure does not make 10:00 hopeless
    assert model.estimate(c, 50, 60, fleet, 10).basis == "node"
    empty = model.estimate(Counts(), 9, 60, (0.3, 0.6), 10)
    assert (empty.basis, empty.p_stay, empty.p_available) == ("fleet", 0.3, 0.6)


def test_the_placement_term_needs_a_busy_node_to_become_idle_first():
    e = model.Estimate(p_stay=0.8, p_available=0.5, hour=0, horizon_min=60, samples=10, basis="hour")
    assert model.availability(e, True) == 0.8
    assert model.availability(e, False) == pytest.approx(0.4)
    assert model.availability(e, None) == pytest.approx(0.4)


# --- Offline evaluation (S6.2) and the live accuracy record (S6.4) ---------------------------------------------
def test_outcomes_match_the_counts():
    s = series(MON + timedelta(hours=9), [True] * 90 + [False] * 30 + [True] * 10)
    out = outcomes(s, 60, 60, 180)
    n, k = model.count(s, [60], 60, 180, UTC).horizon(60)
    assert len(out) == sum(n) and sum(y for _, y in out) == sum(k)


@pytest.fixture
def synthetic_store(tmp_path):
    from idlemesh_coord.predictor import synthetic
    from idlemesh_coord.store import Store
    store = Store(str(tmp_path / "s.sqlite3"))
    kinds = {"n_office": "office", "n_lab": "lab", "n_server": "server"}
    start = datetime(2026, 9, 7, tzinfo=UTC)  # a Monday
    assert synthetic.fleet(store, start, 21, kinds, tz=UTC) > 50_000
    return store, list(kinds), start


def test_hour_of_week_beats_a_flat_rate_on_a_weekly_routine(synthetic_store):
    from idlemesh_coord.predictor import PredictorPolicy, evaluate
    store, nodes, start = synthetic_store
    r = evaluate(store, nodes, PredictorPolicy(window_days=14), start + timedelta(days=14), start + timedelta(days=21),
                 60, UTC)
    assert r["n"] > 10_000 and r["brier"] < r["brier_node_rate"] < r["brier_fleet_rate"]
    assert r["nodes"]["n_office"]["skill_vs_node"] > 0.5  # an office routine is what the model is for
    assert all(abs(b["mean_p"] - b["observed"]) < 0.25 for b in r["calibration"] if b["n"] > 500)
    assert sum(b["n"] for b in r["calibration"]) == r["n"]


def test_accuracy_is_scored_out_of_sample_and_stored_per_node_and_fleet(synthetic_store):
    from idlemesh_coord.predictor import PredictorPolicy, record_accuracy
    store, nodes, start = synthetic_store
    at = start + timedelta(days=20)
    out = record_accuracy(store, nodes, PredictorPolicy(window_days=14, horizons_min=(30, 60)), at, UTC)
    rows = store.all("SELECT * FROM predictor_accuracy ORDER BY node, horizon_min")
    assert out["rows"] == len(rows) == 8  # (fleet + 3 nodes) x 2 horizons
    fleet = [r for r in rows if r["node"] == "*"]
    assert {r["horizon_min"] for r in fleet} == {30, 60} and all(0 <= r["mean_p"] <= 1 for r in fleet)
    # Running again for the same moment replaces, never duplicates.
    record_accuracy(store, nodes, PredictorPolicy(window_days=14, horizons_min=(30, 60)), at, UTC)
    assert len(store.all("SELECT * FROM predictor_accuracy")) == 8
