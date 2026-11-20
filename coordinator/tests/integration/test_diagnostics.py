"""Crash reports (S4.3), diagnostic bundles (S4.4) and the remote log level (S4.5), through the API."""
import json
import uuid
from datetime import datetime, timezone

import pytest
from conftest import GB

from idlemesh_coord import obs

CAPS = {"os": "linux", "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
        "allocatable_bytes": 4 * GB, "mem_bandwidth_gbps": 20, "rtt_ms": 2, "power": "ac"}
DIAG = {"idle_data": True, "diagnostics": True, "policy_version": "v0"}


def _device(client, user, consent=DIAG, name="Node"):
    code = user.post("/api/v1/devices/enrollment-codes").json()["code"]
    r = client.post("/api/v1/devices/enroll", json={"code": code, "name": name, "public_key": "k", "capabilities": CAPS,
                                                   "policy": {"memory_cap_bytes": 4 * GB}, "consent": consent})
    assert r.status_code == 201, r.text
    d = r.json()
    d["auth"] = {"Authorization": f"Bearer {d['device_token']}"}
    return d


def _beat(client, dev, **extra):
    r = client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat", headers=dev["auth"],
                    json={"capabilities": CAPS, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _crash(kind="agent", **kw):
    return {"crash_id": uuid.uuid4().hex, "kind": kind, "ts": datetime.now(timezone.utc).isoformat(),
            "version": "0.1.0", "build_hash": "abc123def456", "os": "linux", "arch": "x86_64",
            "message": "called `Option::unwrap()` on a `None` value", "location": "src/main.rs:42:9",
            "backtrace": "0: idlemesh_agent::run\n1: main", "config": {"name": "Node", "consent": DIAG}, **kw}


def test_crash_reports_are_stored_once_and_visible_to_owner_and_admins(client, group):
    alice, bob, gid = group["alice"], group["bob"], group["group"]["group_id"]
    dev = _device(client, bob)
    obs.take_error_counts()
    obs.install_error_counter()
    c = _crash()
    r = client.post(f"/api/v1/devices/{dev['device_id']}/crash-reports", headers=dev["auth"], json=c)
    assert r.status_code == 201 and r.json()["duplicate"] is False
    # The agent did not hear the reply and uploads again: one report.
    again = client.post(f"/api/v1/devices/{dev['device_id']}/crash-reports", headers=dev["auth"], json=c)
    assert again.json()["duplicate"] is True
    worker = _crash("worker", exit_status="exit code: 139", log_tail=["ggml_abort: out of memory"], location=None)
    client.post(f"/api/v1/devices/{dev['device_id']}/crash-reports", headers=dev["auth"], json=worker)
    assert obs.take_error_counts() == {"AGENT_PANIC": 1, "WORKER_CRASHED": 1}  # counted for the error panel

    for user in (bob, alice):  # the owner and an administrator
        listing = user.get(f"/api/v1/groups/{gid}/devices/{dev['device_id']}/crash-reports").json()["crash_reports"]
        assert {x["kind"] for x in listing} == {"agent", "worker"}
    full = alice.get(f"/api/v1/groups/{gid}/devices/{dev['device_id']}/crash-reports/{worker['crash_id']}").json()
    assert full["log_tail"] == ["ggml_abort: out of memory"] and "exit code: 139" in full["summary"]


def test_other_members_cannot_see_someone_elses_diagnostics(client, group):
    alice, bob, gid = group["alice"], group["bob"], group["group"]["group_id"]
    alices = _device(client, alice)
    assert bob.get(f"/api/v1/groups/{gid}/devices/{alices['device_id']}/crash-reports").status_code == 404
    assert bob.post(f"/api/v1/groups/{gid}/devices/{alices['device_id']}/diagnostics").status_code == 404
    assert bob.post(f"/api/v1/groups/{gid}/devices/{alices['device_id']}/log-level",
                    json={"level": "debug", "minutes": 5}).status_code == 404


def test_everything_needs_diagnostics_consent(client, group):
    alice, gid = group["alice"], group["group"]["group_id"]
    dev = _device(client, alice, consent={"idle_data": True, "diagnostics": False})
    base = f"/api/v1/groups/{gid}/devices/{dev['device_id']}"
    for r in (client.post(f"/api/v1/devices/{dev['device_id']}/crash-reports", headers=dev["auth"], json=_crash()),
              alice.post(f"{base}/diagnostics"),
              alice.post(f"{base}/log-level", json={"level": "debug", "minutes": 5})):
        assert r.status_code == 409 and r.json()["error"]["code"] == "DIAGNOSTICS_NO_CONSENT", r.text
    assert "diagnostics_requests" not in _beat(client, dev)


def test_bundle_request_reaches_the_node_and_comes_back(client, group):
    alice, gid = group["alice"], group["group"]["group_id"]
    dev = _device(client, alice)
    base = f"/api/v1/groups/{gid}/devices/{dev['device_id']}"
    bid = alice.post(f"{base}/diagnostics").json()["bundle_id"]
    assert _beat(client, dev)["diagnostics_requests"] == [bid]
    bundle = {"versions": {"agent": "0.1.0", "build_hash": "abc"}, "logs": {"idlemesh-agent.log": ["{}"]},
              "health": {"cpu_util": 0.1}, "config": {"name": "Node"}}
    r = client.post(f"/api/v1/devices/{dev['device_id']}/diagnostics/{bid}", headers=dev["auth"], json=bundle)
    assert r.status_code == 200, r.text
    assert "diagnostics_requests" not in _beat(client, dev)  # answered: no longer requested
    got = alice.get(f"{base}/diagnostics/{bid}").json()
    assert got["state"] == "uploaded" and got["bundle"] == bundle and got["size_bytes"] > 0
    # A second upload for the same request, or one for a request that never existed, is refused.
    assert client.post(f"/api/v1/devices/{dev['device_id']}/diagnostics/{bid}", headers=dev["auth"],
                       json=bundle).status_code == 404
    assert client.post(f"/api/v1/devices/{dev['device_id']}/diagnostics/diag_nope", headers=dev["auth"],
                       json=bundle).status_code == 404


def test_a_bundle_must_be_a_json_object_with_versions(client, group):
    alice, gid = group["alice"], group["group"]["group_id"]
    dev = _device(client, alice)
    bid = alice.post(f"/api/v1/groups/{gid}/devices/{dev['device_id']}/diagnostics").json()["bundle_id"]
    url = f"/api/v1/devices/{dev['device_id']}/diagnostics/{bid}"
    assert client.post(url, headers=dev["auth"], content=b"not json").status_code == 400
    assert client.post(url, headers=dev["auth"], json=["x"]).status_code == 400
    assert client.post(url, headers=dev["auth"], json={"logs": {}}).status_code == 400


def test_another_device_cannot_answer_a_request(client, group):
    alice, gid = group["alice"], group["group"]["group_id"]
    a, b = _device(client, alice, name="A"), _device(client, alice, name="B")
    bid = alice.post(f"/api/v1/groups/{gid}/devices/{a['device_id']}/diagnostics").json()["bundle_id"]
    r = client.post(f"/api/v1/devices/{b['device_id']}/diagnostics/{bid}", headers=b["auth"],
                    json={"versions": {}})
    assert r.status_code == 404


def test_log_level_window_opens_and_closes(client, group):
    alice, gid = group["alice"], group["group"]["group_id"]
    dev = _device(client, alice)
    base = f"/api/v1/groups/{gid}/devices/{dev['device_id']}"
    r = alice.post(f"{base}/log-level", json={"level": "debug", "minutes": 15})
    assert r.status_code == 200 and r.json()["level"] == "debug"
    window = _beat(client, dev)["log_level"]
    assert window["level"] == "debug" and 14 * 60 < window["remaining_s"] <= 15 * 60
    assert alice.post(f"{base}/log-level", json={"level": "debug", "minutes": 0}).json()["until"] is None
    assert "log_level" not in _beat(client, dev)
    assert alice.post(f"{base}/log-level", json={"level": "debug", "minutes": 241}).status_code == 400


def test_heartbeat_carries_governor_settings_and_stores_the_level(client, group):
    alice, gid = group["alice"], group["group"]["group_id"]
    dev = _device(client, alice)
    reply = _beat(client, dev, contribution={"level": 0.42, "reason": "idle_ramp"}, log_level="info")
    assert set(reply["contribution"]) == {"idle_after_s", "ramp_s", "yield_level", "max_cpu_fraction", "windows"}
    from idlemesh_coord.api import app as _  # noqa: F401 - the app module is what stores capabilities
    caps = json.loads(client.app.state.store.one("SELECT capabilities FROM devices WHERE id = ?",
                                                 (dev["device_id"],))["capabilities"])
    assert caps["contribution"] == {"level": 0.42, "reason": "idle_ramp"} and caps["log_level"] == "info"


@pytest.mark.parametrize("bad", [{"crash_id": "short"}, {"kind": "kernel"}, {"secret_token": "x"}])
def test_malformed_crash_reports_are_refused(client, group, bad):
    dev = _device(client, group["alice"])
    r = client.post(f"/api/v1/devices/{dev['device_id']}/crash-reports", headers=dev["auth"], json={**_crash(), **bad})
    assert r.status_code == 400
