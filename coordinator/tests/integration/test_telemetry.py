"""Telemetry ingest, consent and retention on the coordinator (S2.3, S2.4, S2.6)."""
import sqlite3
from datetime import datetime, timedelta, timezone

from conftest import GB, enroll

from idlemesh_coord import telemetry
from idlemesh_coord.store import Store

CAPS = {"os": "windows", "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
        "allocatable_bytes": 4 * GB, "mem_bandwidth_gbps": 50, "rtt_ms": 2, "power": "ac"}
CONSENT = {"idle_data": True, "diagnostics": False, "policy_version": "v0", "recorded_at": "2026-11-19T00:00:00Z"}


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")


def _sample(minutes_ago: float, state="idle", secs=900) -> dict:
    return {"kind": "idle", "ts": _ts(minutes_ago), "seconds_since_input": secs, "session_locked": False,
            "cpu_util": 0.05, "mem_free_gb": 12.5, "on_battery": False, "battery_pct": None, "state": state}


def _batch(records, dropped=0) -> dict:
    return {"schema_version": 1, "records": records, "dropped_since_last": dropped}


def _device(client, user, consent=CONSENT) -> dict:
    code = user.post("/api/v1/devices/enrollment-codes").json()["code"]
    body = {"code": code, "name": "Dev PC", "public_key": "k", "capabilities": CAPS,
            "policy": {"memory_cap_bytes": 4 * GB}}
    if consent is not None:
        body["consent"] = consent
    r = client.post("/api/v1/devices/enroll", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _upload(client, dev, body):
    return client.post(f"/api/v1/devices/{dev['device_id']}/telemetry", json=body,
                       headers={"Authorization": f"Bearer {dev['device_token']}"})


def test_samples_and_state_changes_are_stored_and_retries_are_ignored(client, group):
    alice = group["alice"]
    dev = _device(client, alice)
    records = [_sample(3, "active", 5),
               {"kind": "state", "ts": _ts(2), "from": "active", "to": "idle"},
               _sample(1)]
    r = _upload(client, dev, _batch(records))
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3, "duplicates": 0}
    # The agent did not hear the reply and sends the same batch again.
    assert _upload(client, dev, _batch(records)).json() == {"accepted": 0, "duplicates": 3}

    gid = group["group"]["group_id"]
    t = alice.get(f"/api/v1/groups/{gid}/devices/{dev['device_id']}/idle?hours=1").json()
    assert [s["state"] for s in t["samples"]] == ["active", "idle"]
    assert t["samples"][1]["seconds_since_input"] == 900 and t["samples"][1]["session_locked"] is False
    assert t["events"] == [{"ts": records[1]["ts"], "from_state": "active", "to_state": "idle"}]
    assert t["dropped"] == 0


def test_node_ids_are_pseudonymized_in_storage(client, group, app):
    dev = _device(client, group["alice"])
    _upload(client, dev, _batch([_sample(1)]))
    rows = app.state.store.all("SELECT node FROM idle_samples")
    assert len(rows) == 1
    assert rows[0]["node"] != dev["device_id"] and rows[0]["node"].startswith("n_")
    assert rows[0]["node"] == app.state.node_keys.key(dev["device_id"])


def test_telemetry_is_refused_without_consent_until_the_owner_grants_it(client, group):
    dev = _device(client, group["alice"], consent=None)
    r = _upload(client, dev, _batch([_sample(1)]))
    assert r.status_code == 403 and r.json()["error"]["code"] == "TELEMETRY_NO_CONSENT"
    # Consent withheld explicitly is the same.
    no = _device(client, group["alice"], consent={**CONSENT, "idle_data": False})
    assert _upload(client, no, _batch([_sample(1)])).status_code == 403
    # The owner consents on the device; the next heartbeat carries the record.
    hb = client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat", json={"capabilities": CAPS, "consent": CONSENT},
                     headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert hb.status_code == 200, hb.text
    assert _upload(client, dev, _batch([_sample(1)])).status_code == 200


def test_heartbeat_tells_agents_the_site_telemetry_settings(client, group):
    dev = enroll(client, group["alice"], "Laptop", 8, 4, 50, 2)
    hb = client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat",
                     json={"capabilities": dev["capabilities"], "node_state": "idle",
                           "telemetry": {"buffer_bytes": 1234, "dropped": 0}},
                     headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert hb.json()["telemetry"] == {"interval_s": 60, "idle_after_s": 600}
    gid = group["group"]["group_id"]
    devices = group["alice"].get(f"/api/v1/groups/{gid}/devices").json()["devices"]
    assert devices[0]["node_state"] == "idle"


def test_invalid_records_are_refused_whole(client, group):
    dev = _device(client, group["alice"])
    bad = {**_sample(1), "cpu_util": 3.0}
    r = _upload(client, dev, _batch([_sample(2), bad]))
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_FIELDS"
    naive = {**_sample(1), "ts": "2026-11-19T10:00:00"}  # no time zone
    assert _upload(client, dev, _batch([naive])).status_code == 400
    prompt = {**_sample(1), "prompt": "never accepted"}  # unknown fields, such as text, are refused
    assert _upload(client, dev, _batch([prompt])).status_code == 400
    assert _upload(client, dev, {"schema_version": 2, "records": []}).status_code == 400


def test_drops_reported_by_the_agent_are_recorded(client, group):
    alice = group["alice"]
    dev = _device(client, alice)
    _upload(client, dev, _batch([_sample(1)], dropped=42))
    gid = group["group"]["group_id"]
    assert alice.get(f"/api/v1/groups/{gid}/devices/{dev['device_id']}/idle").json()["dropped"] == 42


def test_only_the_owner_or_an_admin_sees_a_timeline(client, group):
    alice, bob = group["alice"], group["bob"]
    gid = group["group"]["group_id"]
    bobs = _device(client, bob)
    alices = _device(client, alice)
    assert bob.get(f"/api/v1/groups/{gid}/devices/{bobs['device_id']}/idle").status_code == 200
    assert bob.get(f"/api/v1/groups/{gid}/devices/{alices['device_id']}/idle").status_code == 404
    assert alice.get(f"/api/v1/groups/{gid}/devices/{bobs['device_id']}/idle").status_code == 200  # admin


def test_retention_removes_old_telemetry(client, group, app):
    dev = _device(client, group["alice"])
    _upload(client, dev, _batch([_sample(60 * 24 * 100), _sample(1)]))
    assert telemetry.prune(app.state.store, 90) == 1
    assert len(app.state.store.all("SELECT * FROM idle_samples")) == 1


def test_a_version_1_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES (1);
        CREATE TABLE groups (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, fingerprint TEXT NOT NULL,
          policy_version INTEGER NOT NULL DEFAULT 1, retention_days INTEGER NOT NULL DEFAULT 30,
          created_at TEXT NOT NULL);
        CREATE TABLE devices (id TEXT PRIMARY KEY, group_id TEXT NOT NULL, owner_id TEXT NOT NULL, name TEXT NOT NULL,
          public_key TEXT NOT NULL, token_verifier TEXT NOT NULL, capabilities TEXT NOT NULL,
          capabilities_rev INTEGER NOT NULL DEFAULT 1, policy TEXT NOT NULL, policy_rev INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL, enrolled_at TEXT NOT NULL, last_seen TEXT);
    """)
    db.close()
    store = Store(str(path))
    assert "consent" in {r[1] for r in store.all("PRAGMA table_info(devices)")}
    assert store.one("SELECT version FROM schema_version")[0] == 2
    assert store.all("SELECT * FROM idle_samples") == []
    Store(str(path))  # opening again is a no-op
