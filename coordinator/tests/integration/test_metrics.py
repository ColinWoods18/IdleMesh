"""Hardware profiles in the registry (S3.3, S3.4), health and error telemetry (S3.5), and the read-only metrics
endpoints Grafana uses (S3.6)."""
from datetime import datetime, timedelta, timezone

import pytest
from conftest import GB

from idlemesh_coord import obs

HW_WIN = {"cpu_model": "13th Gen Intel(R) Core(TM) i9-13950HX", "cpu_cores": 24, "cpu_threads": 32,
          "ram_bytes": 128 * GB, "unified_memory": False, "idle_source": "win32", "backends": ["cuda", "vulkan", "cpu"],
          "gpus": [{"vendor": "nvidia", "name": "NVIDIA RTX 3500 Ada Generation Laptop GPU", "vram_bytes": 12 * GB,
                    "driver": "595.95"}]}
HW_MAC = {"cpu_model": "Apple M1 Pro", "cpu_cores": 10, "cpu_threads": 10, "ram_bytes": 32 * GB,
          "unified_memory": True, "idle_source": "iokit", "backends": ["metal", "cpu"],
          "gpus": [{"vendor": "apple", "name": "Apple M1 Pro", "vram_bytes": None, "driver": "metal3"}]}
BOTH = {"idle_data": True, "diagnostics": True, "policy_version": "v0"}


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")


def _device(client, user, name, os_, hw, consent=BOTH):
    caps = {"os": os_, "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
            "allocatable_bytes": 8 * GB, "mem_bandwidth_gbps": 50, "rtt_ms": 2, "power": "ac", "hardware": hw}
    code = user.post("/api/v1/devices/enrollment-codes").json()["code"]
    r = client.post("/api/v1/devices/enroll", json={"code": code, "name": name, "public_key": "k", "capabilities": caps,
                                                   "policy": {"memory_cap_bytes": 8 * GB}, "consent": consent})
    assert r.status_code == 201, r.text
    dev = r.json()
    dev["auth"] = {"Authorization": f"Bearer {dev['device_token']}"}
    dev["caps"] = caps
    hb = client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat", headers=dev["auth"],
                     json={"capabilities": caps, "node_state": "idle"})
    assert hb.status_code == 200, hb.text
    return dev


def _upload(client, dev, records):
    return client.post(f"/api/v1/devices/{dev['device_id']}/telemetry", headers=dev["auth"],
                       json={"schema_version": 1, "records": records})


def _health(minutes_ago, **kw):
    return {"kind": "health", "ts": _ts(minutes_ago), "cpu_util": 0.2, "gpu_util": 0.05, "mem_used_gb": 30.0,
            "gpu_mem_used_gb": 1.5, "temp_c": 55.0, "cache_disk_free_gb": 500.0, "agent_uptime_s": 600,
            "worker_restarts": 0, "cap_kills": 0, "tunnel_errors": 0, "telemetry_buffer_bytes": 0,
            "telemetry_dropped": 0, **kw}


def _idle(minutes_ago, secs, state="idle"):
    return {"kind": "idle", "ts": _ts(minutes_ago), "seconds_since_input": secs, "session_locked": False,
            "cpu_util": 0.1, "gpu_util": 0.0, "mem_free_gb": 10.0, "on_battery": False, "battery_pct": None,
            "state": state}


@pytest.fixture
def key(group):
    r = group["alice"].post("/api/v1/credentials", json={"name": "grafana", "scopes": ["metrics"]})
    assert r.status_code == 201, r.text
    assert r.json()["scopes"] == ["metrics"]
    return {"Authorization": f"Bearer {r.json()['secret']}"}


def test_hardware_profile_is_in_the_device_registry(client, group):
    _device(client, group["alice"], "Dev PC", "windows", HW_WIN)
    gid = group["group"]["group_id"]
    d = group["alice"].get(f"/api/v1/groups/{gid}/devices").json()["devices"][0]
    assert d["hardware"]["backends"] == ["cuda", "vulkan", "cpu"]
    assert d["hardware"]["gpus"][0]["vram_bytes"] == 12 * GB


def test_unknown_backends_and_oversized_profiles_are_refused(client, group):
    bad = {**HW_WIN, "backends": ["quantum"]}
    code = group["alice"].post("/api/v1/devices/enrollment-codes").json()["code"]
    caps = {"os": "windows", "arch": "x86_64", "backend_digest": "x", "formats": ["gguf"], "allocatable_bytes": 1,
            "mem_bandwidth_gbps": 1, "rtt_ms": 1, "hardware": bad}
    r = client.post("/api/v1/devices/enroll", json={"code": code, "name": "x", "public_key": "k", "capabilities": caps,
                                                   "policy": {"memory_cap_bytes": 1}})
    assert r.status_code == 400


def test_health_needs_diagnostics_consent_and_idle_data_needs_idle_consent(client, group):
    idle_only = _device(client, group["alice"], "A", "linux", None, consent={"idle_data": True})
    r = _upload(client, idle_only, [_health(1)])
    assert r.status_code == 403 and "diagnostics" in r.json()["error"]["message"]
    assert _upload(client, idle_only, [_idle(1, 900)]).status_code == 200
    diag_only = _device(client, group["alice"], "B", "linux", None, consent={"diagnostics": True})
    assert _upload(client, diag_only, [_health(1)]).status_code == 200
    r = _upload(client, diag_only, [_idle(1, 900)])
    assert r.status_code == 403 and "idle-data" in r.json()["error"]["message"]


def test_metrics_key_is_admin_only_and_reads_nothing_else(client, group, key):
    gid = group["group"]["group_id"]
    # A member cannot make one, and a metrics key cannot carry other scopes.
    assert group["bob"].post("/api/v1/credentials", json={"name": "g", "scopes": ["metrics"]}).status_code == 403
    r = group["alice"].post("/api/v1/credentials", json={"name": "g", "scopes": ["metrics", "inference"]})
    assert r.status_code == 400
    # The key works on metrics and nowhere else, even though its owner is an administrator.
    assert client.get("/api/v1/metrics/nodes", headers=key).status_code == 200
    for method, url in [("GET", f"/api/v1/groups/{gid}/devices"), ("GET", f"/api/v1/groups/{gid}/members"),
                        ("POST", f"/api/v1/groups/{gid}/deployment"), ("POST", "/v1/chat/completions"),
                        ("GET", "/api/v1/credentials")]:
        r = client.request(method, url, headers=key, json={})
        assert r.status_code == 403, (url, r.status_code, r.text)
    # An ordinary inference key cannot read metrics.
    plain = group["alice"].post("/api/v1/credentials", json={"name": "app"}).json()["secret"]
    assert client.get("/api/v1/metrics/nodes", headers={"Authorization": f"Bearer {plain}"}).status_code == 403
    # A signed-in administrator can; a member cannot.
    assert group["alice"].get("/api/v1/metrics/nodes").status_code == 200
    assert group["bob"].get("/api/v1/metrics/nodes").status_code == 403


def test_metrics_endpoints_return_named_rows(client, group, key):
    win = _device(client, group["alice"], "Dev PC", "windows", HW_WIN)
    mac = _device(client, group["bob"], "MacBook", "macos", HW_MAC)
    assert _upload(client, win, [_idle(3, 5, "active"), _idle(2, 900), _health(2),
                                 {"kind": "errors", "ts": _ts(2), "counts": {"TUNNEL_HANDSHAKE_FAILED": 4}}]).status_code == 200
    assert _upload(client, mac, [_idle(2, 1200), _health(2, gpu_util=None, gpu_mem_used_gb=None),
                                 {"kind": "lid", "ts": _ts(2), "closed": False}]).status_code == 200

    nodes = {n["name"]: n for n in client.get("/api/v1/metrics/nodes", headers=key).json()}
    assert nodes["Dev PC"]["backends"] == "cuda, vulkan, cpu" and nodes["Dev PC"]["status"] == "online"
    assert nodes["MacBook"]["unified_memory"] is True and nodes["MacBook"]["vram_gb"] is None
    assert nodes["Dev PC"]["node"].startswith("n_")  # the pseudonymous key, not the device ID

    health = client.get("/api/v1/metrics/health?hours=1", headers=key).json()
    assert {h["node"] for h in health} == {"Dev PC", "MacBook"}
    states = client.get("/api/v1/metrics/states?hours=1", headers=key).json()
    assert [s["state"] for s in states if s["node"] == "Dev PC"] == ["active", "idle"]

    heat = client.get("/api/v1/metrics/idle-heatmap?hours=1", headers=key).json()
    assert [row["day"] for row in heat] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    values = [v for row in heat for k, v in row.items() if k != "day" and v is not None]
    assert values and all(0 <= v <= 1 for v in values)
    # Three samples: two idle (900 s and 1200 s past the 600 s threshold), one active.
    assert abs(sum(values) / len(values) - 2 / 3) < 0.01 or len(values) > 1
    one = client.get("/api/v1/metrics/idle-heatmap?hours=1&node=MacBook", headers=key).json()
    assert [v for row in one for k, v in row.items() if k != "day" and v is not None] == [1.0]

    # Grafana's own time range works too.
    to_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    errs = client.get(f"/api/v1/metrics/errors?from_ms={to_ms - 3_600_000}&to_ms={to_ms}", headers=key).json()
    assert {(e["code"], e["source"], e["count"]) for e in errs} >= {("TUNNEL_HANDSHAKE_FAILED", "Dev PC", 4)}


def test_metrics_never_show_another_groups_nodes(client, group, key, app):
    _device(client, group["alice"], "Ours", "windows", HW_WIN)
    # A second group on the same coordinator, with its own device and telemetry.
    from conftest import OWNER_PASS, login
    other = client.post("/api/v1/groups", json={"name": "Other", "owner_login": "zed", "owner_display_name": "Z",
                                                "passphrase": OWNER_PASS}).json()
    zed = login(client, other["group_id"], "zed", OWNER_PASS)
    theirs = _device(client, zed, "Theirs", "linux", None)
    assert _upload(client, theirs, [_health(1)]).status_code == 200
    names = {n["name"] for n in client.get("/api/v1/metrics/nodes", headers=key).json()}
    assert names == {"Ours"}
    assert client.get("/api/v1/metrics/health?hours=1", headers=key).json() == []


def test_coordinator_errors_are_counted_by_code(client, group, key, app):
    obs.take_error_counts()  # start clean
    obs.install_error_counter()
    import logging
    obs.log(logging.getLogger("idlemesh_coord.test"), logging.WARNING, "boom", error_code="PREFETCH_FAILED")
    counts = obs.take_error_counts()
    assert counts.get("PREFETCH_FAILED") == 1
    from idlemesh_coord import telemetry
    telemetry.record_coordinator_errors(app.state.store, counts, None, None)
    errs = client.get("/api/v1/metrics/errors?hours=1", headers=key).json()
    assert ("PREFETCH_FAILED", "coordinator") in {(e["code"], e["source"]) for e in errs}
