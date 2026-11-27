"""scripts/export_dataset.py (S6.5): consent, anonymity and reproducibility of the research export."""
import csv
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import GB

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("export_dataset", ROOT / "scripts" / "export_dataset.py")
exporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(exporter)

CAPS = {"os": "windows", "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
        "allocatable_bytes": 4 * GB, "mem_bandwidth_gbps": 50, "rtt_ms": 2, "power": "ac"}


def _consent(idle, diag):
    return {"idle_data": idle, "diagnostics": diag, "policy_version": "v0", "recorded_at": "2026-11-19T00:00:00Z"}


def _device(client, user, name, consent):
    code = user.post("/api/v1/devices/enrollment-codes").json()["code"]
    r = client.post("/api/v1/devices/enroll", json={"code": code, "name": name, "public_key": "k", "capabilities": CAPS,
                                                    "policy": {"memory_cap_bytes": 4 * GB}, "consent": consent})
    assert r.status_code == 201, r.text
    return r.json()


def _upload(client, dev, records):
    r = client.post(f"/api/v1/devices/{dev['device_id']}/telemetry", json={"schema_version": 1, "records": records},
                    headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 200, r.text


def _idle(t, state="idle"):
    return {"kind": "idle", "ts": t.isoformat(timespec="milliseconds"), "seconds_since_input": 900,
            "session_locked": False, "cpu_util": 0.05, "mem_free_gb": 12.5, "on_battery": False, "battery_pct": None,
            "state": state}


def test_export_is_consented_anonymous_and_reproducible(client, group, app, tmp_path):
    alice = group["alice"]
    both = _device(client, alice, "Alice's desk", _consent(True, True))
    idle_only = _device(client, alice, "Bob's desk", _consent(True, False))
    gone = _device(client, alice, "Old desk", _consent(True, True))
    t0 = datetime(2026, 11, 23, 9, tzinfo=timezone.utc)
    for d in (both, idle_only, gone):
        _upload(client, d, [_idle(t0 + timedelta(minutes=i)) for i in range(30)])
    _upload(client, both, [{"kind": "net_edge", "ts": t0.isoformat(), "dst": "coordinator", "method": "tunnel",
                            "rtt_ms_p50": 1.0, "rtt_ms_p95": 2.0, "samples": 5}])
    with app.state.store.tx() as c:
        c.execute("UPDATE devices SET status = 'revoked' WHERE id = ?", (gone["device_id"],))

    db = Path(app.state.settings.db_path)
    m1 = exporter.export(db, tmp_path / "a", "seed-1")
    assert m1["nodes"] == 2 and m1["files"]["idle_samples.csv"]["rows"] == 60  # the revoked device is left out
    assert m1["files"]["net_edges.csv"]["rows"] == 1
    rows = list(csv.DictReader((tmp_path / "a" / "idle_samples.csv").open(encoding="utf-8")))
    assert {r["node"] for r in rows} == {"node_01", "node_02"} and rows[0]["ts"].endswith("Z")
    edge = next(csv.DictReader((tmp_path / "a" / "net_edges.csv").open(encoding="utf-8")))
    assert edge["peer"] == "coordinator"

    # Nothing that names a person, a device or the coordinator's node keys leaves.
    text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "a").iterdir())
    keys = app.state.node_keys
    for d in (both, idle_only, gone):
        assert d["device_id"] not in text and keys.key(d["device_id"]) not in text
    assert "desk" not in text and "org_id" not in text and "received_at" not in text

    # The same seed and range give the same bytes; the range cuts samples.
    m2 = exporter.export(db, tmp_path / "b", "seed-1")
    assert m2["files"] == m1["files"]
    cut = exporter.export(db, tmp_path / "c", "seed-1", "2026-11-23T09:10:00", "2026-11-23T09:20:00")
    assert cut["files"]["idle_samples.csv"]["rows"] == 20
    assert json.loads((tmp_path / "a" / "manifest.json").read_text())["format"] == "idlemesh-dataset/1"


def test_without_idle_consent_a_node_exports_no_samples(client, group, app, tmp_path):
    dev = _device(client, group["alice"], "Desk", _consent(True, True))
    _upload(client, dev, [_idle(datetime(2026, 11, 23, 9, tzinfo=timezone.utc))])
    # The owner withdraws idle-data consent; samples already stored are not exported.
    with app.state.store.tx() as c:
        c.execute("UPDATE devices SET consent = ? WHERE id = ?", (json.dumps(_consent(False, True)), dev["device_id"]))
    m = exporter.export(Path(app.state.settings.db_path), tmp_path / "x", "s")
    assert m["files"]["idle_samples.csv"]["rows"] == 0
