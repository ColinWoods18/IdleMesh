"""telemetry.py called directly, without the HTTP layer: node keys, consent, ingest, timeline, retention."""
from datetime import datetime, timedelta, timezone

import pytest

from idlemesh_coord import telemetry
from idlemesh_coord.api.schemas import TelemetryBatchIn
from idlemesh_coord.fsperm import is_owner_only
from idlemesh_coord.store import Store


def _at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")


def _idle(minutes_ago: float, **kw) -> dict:
    return {"kind": "idle", "ts": _at(minutes_ago), "seconds_since_input": 700, "session_locked": True,
            "cpu_util": 0.1, "mem_free_gb": 8.0, "on_battery": False, "battery_pct": None, "state": "idle", **kw}


def _batch(records, dropped=0) -> TelemetryBatchIn:
    return TelemetryBatchIn.model_validate({"schema_version": 1, "records": records, "dropped_since_last": dropped})


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.sqlite3"))


# --- node keys -------------------------------------------------------------------------------
def test_node_key_is_stable_per_site_and_hides_the_device_id(tmp_path):
    a = telemetry.NodeKeys(str(tmp_path), pseudonymize=True)
    k = a.key("dev_123")
    assert k.startswith("n_") and len(k) == 26 and "dev_123" not in k
    assert a.key("dev_123") == k != a.key("dev_456")
    # A restarted coordinator reads the same salt, so keys survive restarts.
    assert telemetry.NodeKeys(str(tmp_path), pseudonymize=True).key("dev_123") == k


def test_salt_file_is_owner_only_and_differs_between_sites(tmp_path):
    a = telemetry.NodeKeys(str(tmp_path / "site-a"), pseudonymize=True)
    b = telemetry.NodeKeys(str(tmp_path / "site-b"), pseudonymize=True)
    assert is_owner_only(tmp_path / "site-a" / telemetry.SALT_FILE)
    assert a.key("dev_1") != b.key("dev_1")


def test_without_pseudonymization_the_device_id_is_used_and_no_salt_is_written(tmp_path):
    keys = telemetry.NodeKeys(str(tmp_path), pseudonymize=False)
    assert keys.key("dev_1") == "dev_1"
    assert not (tmp_path / telemetry.SALT_FILE).exists()


# --- consent ---------------------------------------------------------------------------------
@pytest.mark.parametrize("consent,ok", [
    (None, False), ({}, False), ({"idle_data": False, "diagnostics": True}, False),
    ({"idle_data": True}, True), ({"idle_data": True, "diagnostics": False}, True),
])
def test_consent_needs_idle_data_specifically(consent, ok):
    assert telemetry.has_consent(consent) is ok


# --- ingest, timeline, prune -----------------------------------------------------------------
def test_ingest_counts_new_and_repeated_records(store):
    recs = [_idle(3), {"kind": "state", "ts": _at(2), "from": "active", "to": "idle"}, _idle(1)]
    assert telemetry.ingest(store, "n_a", _batch(recs), "org", "site") == {"accepted": 3, "duplicates": 0}
    assert telemetry.ingest(store, "n_a", _batch(recs), "org", "site") == {"accepted": 0, "duplicates": 3}
    # The same timestamps from another node are separate records.
    assert telemetry.ingest(store, "n_b", _batch(recs), "org", "site")["accepted"] == 3
    row = store.one("SELECT org_id, site_id FROM idle_samples WHERE node = 'n_a' LIMIT 1")
    assert (row["org_id"], row["site_id"]) == ("org", "site")


def test_timestamps_in_other_zones_deduplicate_against_utc(store):
    utc = datetime(2026, 11, 19, 12, 0, 0, 123000, tzinfo=timezone.utc)
    est = utc.astimezone(timezone(timedelta(hours=-5)))
    telemetry.ingest(store, "n", _batch([_idle(0, ts=utc.isoformat())]), None, None)
    assert telemetry.ingest(store, "n", _batch([_idle(0, ts=est.isoformat())]), None, None)["duplicates"] == 1


def test_timeline_is_ordered_typed_and_scoped_to_the_node(store):
    telemetry.ingest(store, "n", _batch([_idle(1, state="idle"), _idle(5, state="active", session_locked=None,
                                                                         on_battery=True, battery_pct=40.0)],
                                        dropped=7), None, None)
    telemetry.ingest(store, "other", _batch([_idle(2)]), None, None)
    t = telemetry.timeline(store, "n", datetime.now(timezone.utc) - timedelta(hours=1))
    assert [s["state"] for s in t["samples"]] == ["active", "idle"]  # oldest first
    first = t["samples"][0]
    assert first["session_locked"] is None and first["on_battery"] is True and first["battery_pct"] == 40.0
    assert t["samples"][1]["session_locked"] is True
    assert t["dropped"] == 7 and t["events"] == []


def test_timeline_since_excludes_older_records(store):
    telemetry.ingest(store, "n", _batch([_idle(120), _idle(10)]), None, None)
    t = telemetry.timeline(store, "n", datetime.now(timezone.utc) - timedelta(minutes=30))
    assert len(t["samples"]) == 1


def test_prune_removes_old_samples_events_and_drops_only(store):
    old = 60 * 24 * 40
    telemetry.ingest(store, "n", _batch([_idle(old), _idle(1),
                                         {"kind": "state", "ts": _at(old), "from": None, "to": "idle"}]), None, None)
    with store.tx() as c:
        c.execute("INSERT INTO telemetry_drops (node, at, count) VALUES ('n', '2000-01-01T00:00:00+00:00', 3)")
    assert telemetry.prune(store, 30) == 3
    assert len(store.all("SELECT * FROM idle_samples")) == 1
    assert telemetry.prune(store, 30) == 0


def _health(minutes_ago: float, **kw) -> dict:
    return {"kind": "health", "ts": _at(minutes_ago), "cpu_util": 0.3, "gpu_util": 0.1, "mem_used_gb": 20.5,
            "gpu_mem_used_gb": 3.2, "temp_c": 61.0, "cache_disk_free_gb": 400.0, "agent_uptime_s": 3600,
            "worker_restarts": 1, "cap_kills": 0, "tunnel_errors": 2, "telemetry_buffer_bytes": 1024,
            "telemetry_dropped": 0, **kw}


@pytest.mark.parametrize("consent,kinds,ok", [
    ({"idle_data": True}, {"idle", "state", "lid"}, True),
    ({"idle_data": True}, {"idle", "health"}, False),          # health needs diagnostics consent
    ({"diagnostics": True}, {"health", "errors"}, True),
    ({"diagnostics": True}, {"idle"}, False),                  # diagnostics alone does not cover idle data
    ({"idle_data": True, "diagnostics": True}, {"idle", "lid", "health", "errors"}, True),
])
def test_each_record_kind_needs_its_own_consent(consent, kinds, ok):
    assert telemetry.has_consent(consent, kinds) is ok


def test_lid_health_and_error_records_are_stored_and_deduplicated(store):
    recs = [{"kind": "lid", "ts": _at(5), "closed": True}, _health(4),
            {"kind": "errors", "ts": _at(3), "counts": {"TUNNEL_HANDSHAKE_FAILED": 2, "WORKER_EXITED": 1}}]
    assert telemetry.ingest(store, "n", _batch(recs), "org", "site") == {"accepted": 3, "duplicates": 0}
    assert telemetry.ingest(store, "n", _batch(recs), "org", "site") == {"accepted": 0, "duplicates": 3}
    assert store.one("SELECT kind, to_state FROM node_events")[:] == ("lid", "closed")
    h = store.one("SELECT * FROM node_health")
    assert (h["tunnel_errors"], h["temp_c"], h["org_id"]) == (2, 61.0, "org")
    errs = {r["code"]: r["count"] for r in store.all("SELECT code, count FROM error_events WHERE source = 'agent'")}
    assert errs == {"TUNNEL_HANDSHAKE_FAILED": 2, "WORKER_EXITED": 1}
    # Lid events are not node-state changes in the timeline.
    assert telemetry.timeline(store, "n", datetime.now(timezone.utc) - timedelta(hours=1))["events"] == []


def test_health_without_a_gpu_is_stored_as_absent(store):
    telemetry.ingest(store, "n", _batch([_health(1, gpu_util=None, gpu_mem_used_gb=None, temp_c=None)]), None, None)
    h = store.one("SELECT gpu_util, gpu_mem_used_gb, temp_c FROM node_health")
    assert tuple(h) == (None, None, None)


def test_coordinator_error_counts_get_their_own_rows(store):
    telemetry.record_coordinator_errors(store, {"INVALID_LOGIN": 3}, "org", "site")
    telemetry.record_coordinator_errors(store, {}, None, None)  # nothing to record
    r = store.one("SELECT node, source, code, count FROM error_events")
    assert tuple(r) == (telemetry.COORDINATOR_NODE, "coordinator", "INVALID_LOGIN", 3)


def test_prune_covers_health_and_error_tables(store):
    old = 60 * 24 * 40
    telemetry.ingest(store, "n", _batch([_health(old), _health(1),
                                         {"kind": "errors", "ts": _at(old), "counts": {"X_Y": 1}}]), None, None)
    assert telemetry.prune(store, 30) == 2
    assert len(store.all("SELECT * FROM node_health")) == 1 and store.all("SELECT * FROM error_events") == []


@pytest.mark.parametrize("bad", [
    {"kind": "errors", "ts": _at(1), "counts": {"lower_case": 1}},       # not an error code
    {"kind": "errors", "ts": _at(1), "counts": {"WORKER_EXITED": 0}},    # counts are at least 1
    {"kind": "health", "ts": _at(1), "cpu_util": 2.0},                   # incomplete and out of range
    {"kind": "lid", "ts": _at(1), "closed": "maybe"},
])
def test_malformed_new_records_are_refused(bad):
    with pytest.raises(ValueError):
        _batch([bad])


def test_no_record_type_can_carry_free_text():
    """D-11: telemetry never carries prompt, response or typed text. Unknown fields are refused."""
    with pytest.raises(ValueError):
        _batch([_idle(1, window_title="secret.docx")])
    with pytest.raises(ValueError):
        _batch([{"kind": "state", "ts": _at(1), "from": "idle", "to": "active", "note": "x"}])
    with pytest.raises(ValueError):
        _batch([_health(1, last_log_line="secret.docx opened")])
    with pytest.raises(ValueError):
        _batch([{"kind": "errors", "ts": _at(1), "counts": {"WORKER_EXITED": 1}, "message": "x"}])
