"""Idle samples in, availability estimates out, through the coordinator API (S6.1)."""
from datetime import datetime, timedelta, timezone

from conftest import GB

from idlemesh_coord.api.app import refit_predictor

CAPS = {"os": "windows", "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
        "allocatable_bytes": 4 * GB, "mem_bandwidth_gbps": 50, "rtt_ms": 2, "power": "ac"}
CONSENT = {"idle_data": True, "diagnostics": False, "policy_version": "v0", "recorded_at": "2026-11-19T00:00:00Z"}


def _device(client, user, name) -> dict:
    code = user.post("/api/v1/devices/enrollment-codes").json()["code"]
    r = client.post("/api/v1/devices/enroll", json={"code": code, "name": name, "public_key": "k", "capabilities": CAPS,
                                                    "policy": {"memory_cap_bytes": 4 * GB}, "consent": CONSENT})
    assert r.status_code == 201, r.text
    return r.json()


def _upload(client, dev, states, start):
    recs = [{"kind": "idle", "ts": (start + timedelta(minutes=i)).isoformat(timespec="milliseconds"),
             "seconds_since_input": 900 if s == "idle" else 3, "session_locked": False, "cpu_util": 0.05,
             "mem_free_gb": 12.5, "on_battery": False, "battery_pct": None, "state": s} for i, s in enumerate(states)]
    for i in range(0, len(recs), 5000):
        r = client.post(f"/api/v1/devices/{dev['device_id']}/telemetry",
                        json={"schema_version": 1, "records": recs[i:i + 5000]},
                        headers={"Authorization": f"Bearer {dev['device_token']}"})
        assert r.status_code == 200, r.text


def _beat(client, dev, state):
    r = client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat",
                    json={"capabilities": CAPS, "consent": CONSENT, "node_state": state},
                    headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 200, r.text


def test_a_steady_idle_node_is_predicted_to_stay_idle_and_a_restless_one_is_not(client, group, app):
    alice, gid = group["alice"], group["group"]["group_id"]
    steady, restless = _device(client, alice, "Night desk"), _device(client, alice, "Hot desk")
    start = datetime.now(timezone.utc) - timedelta(days=8)
    minutes = 8 * 24 * 60 - 60
    _upload(client, steady, ["idle"] * minutes, start)
    _upload(client, restless, (["idle"] * 10 + ["active"] * 10) * (minutes // 20), start)
    for d in (steady, restless):
        _beat(client, d, "idle")

    summary = refit_predictor(app)
    assert summary["nodes"] == 2 and summary["samples"] > 20_000
    out = {d["device_id"]: alice.get(f"/api/v1/groups/{gid}/devices/{d['device_id']}/availability").json()
           for d in (steady, restless)}
    s, r = out[steady["device_id"]], out[restless["device_id"]]
    assert s["now"]["basis"] == "hour" and s["now"]["horizon_min"] == 60 and s["node_state"] == "idle"
    assert s["availability"] > 0.9 and r["availability"] < 0.2
    assert len(s["week"]) == 168 and s["fitted_at"]
    # A busy node has to become idle first, so it scores lower than the same node idle now.
    _beat(client, steady, "active")
    busy = alice.get(f"/api/v1/groups/{gid}/devices/{steady['device_id']}/availability?horizon_min=25").json()
    assert busy["now"]["horizon_min"] == 30 and busy["availability"] < busy["now"]["p_stay"]


def test_only_group_admins_see_availability(client, group, app):
    gid = group["group"]["group_id"]
    dev = _device(client, group["alice"], "A")
    url = f"/api/v1/groups/{gid}/devices/{dev['device_id']}/availability"
    assert group["bob"].get(url).status_code == 403
    r = group["alice"].get(url)
    assert r.status_code == 200 and r.json()["now"]["basis"] == "fleet"  # nothing fitted yet
    assert group["alice"].get(f"/api/v1/groups/{gid}/devices/d_missing/availability").status_code == 404


def test_placement_picks_the_node_more_likely_to_stay_idle(client, group, app):
    alice, gid = group["alice"], group["group"]["group_id"]
    steady, restless = _device(client, alice, "Night desk"), _device(client, alice, "Hot desk")
    start = datetime.now(timezone.utc) - timedelta(days=8)
    minutes = 8 * 24 * 60 - 60
    _upload(client, steady, ["idle"] * minutes, start)
    _upload(client, restless, (["idle"] * 10 + ["active"] * 10) * (minutes // 20), start)
    for d in (restless, steady):
        _beat(client, d, "idle")
    refit_predictor(app)
    r = alice.post(f"/api/v1/groups/{gid}/deployment", json={"model": "fixture-small"})
    assert r.status_code == 200, r.text
    ex = r.json()["explanation"]
    assert [s["name"] for s in ex["stages"]] == ["Night desk"]
    scores = {s["device_id"]: s["components"]["availability"] for s in ex["scores"]}
    assert scores[steady["device_id"]] > 0.9 > 0.2 > scores[restless["device_id"]]
    listed = {d["name"]: d for d in alice.get(f"/api/v1/groups/{gid}/devices").json()["devices"]}
    assert listed["Night desk"]["availability"] == scores[steady["device_id"]]


def test_predictor_accuracy_reaches_the_metrics_endpoint(client, group, app):
    from idlemesh_coord.api.app import record_predictor_accuracy
    alice = group["alice"]
    dev = _device(client, alice, "Night desk")
    start = datetime.now(timezone.utc) - timedelta(days=3)
    _upload(client, dev, ["idle"] * (3 * 24 * 60 - 30), start)
    out = record_predictor_accuracy(app)
    assert out["rows"] == 6 and out["scored"] > 0  # fleet and the node, three horizons
    rows = alice.get("/api/v1/metrics/predictor-accuracy").json()
    assert {r["node"] for r in rows} == {"fleet", "Night desk"} and {r["horizon_min"] for r in rows} == {60}
    node = next(r for r in rows if r["node"] == "Night desk")
    assert node["observed"] == 1.0 and node["gap"] == round(node["mean_p"] - 1.0, 4)
    assert alice.get("/api/v1/metrics/predictor-accuracy?horizon_min=30").json()[0]["horizon_min"] == 30
    assert group["bob"].get("/api/v1/metrics/predictor-accuracy").status_code == 403
