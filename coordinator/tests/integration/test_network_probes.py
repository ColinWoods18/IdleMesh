"""Bandwidth tests through node tunnels, through the coordinator API (S5.2, S5.3, S5.5).

The tunnel itself is replaced by a stand-in that returns fixed figures; test_bandwidth_agent.py times a real
agent's tunnel. What is checked here: which nodes the sweep tests and when, what is stored, the admin trigger and
its refusals, placement using the figure, and the network metrics for Grafana."""
import asyncio

import pytest
from conftest import GB

from idlemesh_coord.api import netprobe
from idlemesh_coord.store import now
from idlemesh_coord.telemetry import _ts
from idlemesh_coord.transport import TunnelError

CONSENT = {"idle_data": True, "diagnostics": True, "policy_version": "v0", "recorded_at": "2026-11-19T00:00:00Z"}
NO_DIAG = {**CONSENT, "diagnostics": False}
BW = {"up_mbps": 95.0, "down_mbps": 90.0, "mbps": 90.0, "bytes": 8 << 20, "duration_ms": 1500.0}


def _caps(bw=50, rtt=2.0):
    return {"os": "windows", "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
            "allocatable_bytes": 4 * GB, "mem_bandwidth_gbps": bw, "rtt_ms": rtt, "power": "ac",
            "network": {"subnet": "10.1.0.0/24"}}


class FakeTunnels:
    """Stands in for TunnelManager's network calls."""

    def __init__(self, real):
        self.identity, self.real = real.identity, real
        self.calls, self.fail, self.result = [], False, dict(BW)

    def measure_bandwidth(self, remote, cert, nbytes=8 << 20, timeout=60):
        self.calls.append((remote, nbytes))
        if self.fail:
            raise TunnelError(f"bandwidth test through the tunnel at {remote} failed: timeout")
        return dict(self.result)

    def probe(self, remote, cert, timeout=5):
        return None

    def measure_rtt(self, remote, cert, samples=16, timeout=5):
        return {"mean_ms": 1.5, "median_ms": 1.4, "min_ms": 1.2, "p90_ms": 1.9, "p95_ms": 2.0, "samples": 16}

    def open(self, owner, remote, cert):
        return "127.0.0.1:1"

    def close(self, owner):
        pass

    def close_all(self):
        pass


@pytest.fixture
def tunnels(app):
    app.state.net_probe["next_sweep"] = float("inf")  # the tests run sweeps themselves
    fake = FakeTunnels(app.state.tunnels)
    app.state.tunnels = fake
    return fake


def _enroll(client, user, name, consent=CONSENT, bw=50):
    code = user.post("/api/v1/devices/enrollment-codes").json()["code"]
    r = client.post("/api/v1/devices/enroll", json={"code": code, "name": name, "public_key": "k",
                                                    "capabilities": _caps(bw), "policy": {"memory_cap_bytes": 4 * GB},
                                                    "consent": consent})
    assert r.status_code == 201, r.text
    return r.json()


def _beat(client, dev, *, worker=True, level=0.5, reason="idle_ramp", consent=CONSENT, bw=50):
    body = {"capabilities": _caps(bw), "consent": consent, "node_state": "idle",
            "contribution": {"level": level, "reason": reason}}
    if worker:
        body["worker"] = {"state": "running", "endpoint": "192.0.2.10:50443", "rss_bytes": 1, "peak_rss_bytes": 1,
                          "cap_kills": 0, "transport": "mtls"}
    r = client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat", json=body,
                    headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 200, r.text


def _sweep(app):
    asyncio.run(netprobe._sweep_once(app))


def test_the_sweep_tests_a_new_worker_once_and_the_figure_reaches_the_views(client, group, app, tunnels):
    alice, gid = group["alice"], group["group"]["group_id"]
    lab = _enroll(client, alice, "Lab PC")
    _beat(client, lab)
    _sweep(app)
    assert tunnels.calls == [("192.0.2.10:50443", 8 << 20)]
    row = app.state.store.one("SELECT * FROM net_edges WHERE method = 'tunnel_test'")
    assert (row["src"], row["dst"], row["bandwidth_mbps"]) == (app.state.node_keys.key(lab["device_id"]),
                                                               "coordinator", 90.0)
    _sweep(app)  # tested an hour ago: not due again for a week
    assert len(tunnels.calls) == 1

    view = alice.get(f"/api/v1/groups/{gid}/network").json()
    assert view["nodes"][0]["coordinator_mbps"] == 90.0
    devices = alice.get(f"/api/v1/groups/{gid}/devices").json()["devices"]
    assert devices[0]["link_mbps"] == 90.0
    probes = alice.get(f"/api/v1/groups/{gid}/network/probes").json()["devices"]
    assert probes[0]["link_mbps"] == 90.0 and probes[0]["last_test"]["reason"] == "first"
    assert probes[0]["due"] is None and probes[0]["blocked_by"] is None


def test_the_sweep_leaves_alone_nodes_that_cannot_be_tested(client, group, app, tunnels):
    alice = group["alice"]
    busy = _enroll(client, alice, "In use")
    _beat(client, busy, reason="user_activity", level=0.02)  # the owner is at the keyboard (D-12)
    nodiag = _enroll(client, alice, "No consent", consent=NO_DIAG)
    _beat(client, nodiag, consent=NO_DIAG)
    plain = _enroll(client, alice, "No tunnel")
    _beat(client, plain, worker=False)
    _sweep(app)
    assert tunnels.calls == []
    reasons = {d["name"]: d["blocked_by"] for d in
               alice.get(f"/api/v1/groups/{group['group']['group_id']}/network/probes").json()["devices"]}
    assert "governor" in reasons["In use"] and "consented" in reasons["No consent"]
    assert "tunnel" in reasons["No tunnel"]


def test_a_failed_test_is_counted_and_not_retried_for_an_hour(client, group, app, tunnels, caplog):
    lab = _enroll(client, group["alice"], "Lab PC")
    _beat(client, lab)
    tunnels.fail = True
    _sweep(app)
    _sweep(app)
    assert len(tunnels.calls) == 1
    assert any(getattr(r, "error_code", None) == "NET_PROBE_FAILED" for r in caplog.records)
    status = app.state.net_probe["status"][lab["device_id"]]
    assert status["ok"] is False and status["code"] == "NET_PROBE_FAILED"
    assert app.state.store.one("SELECT COUNT(*) AS n FROM net_edges WHERE method = 'tunnel_test'")["n"] == 0


def test_an_admin_can_test_a_link_now_within_the_same_rules(client, group, app, tunnels):
    alice, bob, gid = group["alice"], group["bob"], group["group"]["group_id"]
    lab = _enroll(client, alice, "Lab PC")
    _beat(client, lab)
    url = f"/api/v1/groups/{gid}/devices/{lab['device_id']}/bandwidth-test"
    assert bob.post(url).status_code == 403
    r = alice.post(url)
    assert r.status_code == 200, r.text
    assert (r.json()["mbps"], r.json()["reason"]) == (90.0, "admin")

    tunnels.result = {**BW, "mbps": 20.0, "down_mbps": 20.0}
    assert alice.post(url).json()["degraded"] is True  # below half of 90

    _beat(client, lab, reason="user_activity", level=0.02)
    r = alice.post(url)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NET_PROBE_REFUSED"
    _beat(client, lab)
    tunnels.fail = True
    r = alice.post(url)
    assert r.status_code == 503 and r.json()["error"]["code"] == "NET_PROBE_FAILED"


def _deploy(alice, gid):
    r = alice.post(f"/api/v1/groups/{gid}/deployment", json={"model": "fixture-small", "require_all": True})
    assert r.status_code == 200, r.text
    return {s["name"]: s for s in r.json()["explanation"]["stages"]}, r.json()["explanation"]


def test_placement_plans_with_the_tested_link(client, group, app, tunnels):
    alice, gid = group["alice"], group["group"]["group_id"]
    head = _enroll(client, alice, "Head", bw=800)
    _beat(client, head, worker=False, bw=800)
    lab = _enroll(client, alice, "Lab PC")
    _beat(client, lab)
    tunnels.result = {**BW, "mbps": 100.0}
    _sweep(app)
    stages, expl = _deploy(alice, gid)
    assert (stages["Lab PC"]["link_mbps"], stages["Lab PC"]["link_source"]) == (100.0, "tunnel_test")
    assert len(tunnels.calls) == 1  # a stored figure: no test at load
    # Per-token traffic over a 100 Mbps link: 0.62 MB x 8 / 100 Mbps = 49.6 ms on top of the round trips.
    assert stages["Lab PC"]["est_network_ms"] == round(1.02 * 1.5 + 49.6, 2)
    assert expl["latency_budget"]["links_unmeasured"] == []


def test_a_link_with_no_figure_is_timed_at_load_and_kept_in_memory_without_consent(client, group, app, tunnels):
    alice, gid = group["alice"], group["group"]["group_id"]
    head = _enroll(client, alice, "Head", bw=800)
    _beat(client, head, worker=False, bw=800)
    lab = _enroll(client, alice, "Lab PC", consent=NO_DIAG)
    _beat(client, lab, consent=NO_DIAG)
    stages, expl = _deploy(alice, gid)
    lab_stage = stages["Lab PC"]
    assert tunnels.calls == [("192.0.2.10:50443", netprobe.LOAD_TEST_BYTES)]
    assert lab_stage["link_mbps"] == 90.0 and lab_stage["planned_link_mbps"] is None
    assert expl["planned_latency_budget"]["per_token_ms"] < expl["latency_budget"]["per_token_ms"]
    assert app.state.store.one("SELECT COUNT(*) AS n FROM net_edges WHERE method = 'tunnel_test'")["n"] == 0
    assert app.state.path_mbps[lab["device_id"]][0] == 90.0
    # The sweep never tests a node in a deployment.
    assert netprobe.why_not(app, app.state.store.one("SELECT * FROM devices WHERE id = ?", (lab["device_id"],)),
                            netprobe.busy_devices(app)) is not None


def test_network_metrics_for_grafana(client, group, app, tunnels):
    alice = group["alice"]
    a, b = _enroll(client, alice, "Lab A"), _enroll(client, alice, "Lab B")
    for d in (a, b):
        _beat(client, d)
    _sweep(app)
    _sweep(app)
    from idlemesh_coord.api.app import recompute_clusters
    recompute_clusters(app)

    nodes = alice.get("/api/v1/metrics/network-nodes").json()
    assert {n["name"]: n["link_mbps"] for n in nodes} == {"Lab A": 90.0, "Lab B": 90.0}
    assert nodes[0]["cluster"] == nodes[1]["cluster"] and nodes[0]["cluster_size"] == 2  # relayed: 2 + 2 ms
    gnodes = alice.get("/api/v1/metrics/network-graph?part=nodes").json()
    assert [n["id"] for n in gnodes][0] == "coordinator" and len(gnodes) == 3
    assert gnodes[1]["color"] == gnodes[2]["color"] != "#8f8f8f"
    edges = alice.get("/api/v1/metrics/network-graph?part=edges").json()
    assert sorted(e["kind"] for e in edges) == ["cluster", "head", "head"]
    assert alice.get("/api/v1/metrics/network-graph?part=other").status_code == 400

    series = alice.get("/api/v1/metrics/network-links?method=tunnel_test").json()
    assert {(r["node"], r["peer"], r["bandwidth_mbps"]) for r in series} == {("Lab A", "coordinator", 90.0),
                                                                             ("Lab B", "coordinator", 90.0)}
    assert all(r["method"] == "heartbeat" for r in alice.get("/api/v1/metrics/network-links?method=heartbeat").json())
    with app.state.store.tx() as c:
        for code, n in (("NET_PROBE_FAILED", 2), ("INTERNAL", 5)):
            c.execute("INSERT INTO error_events (node, ts, source, code, count) VALUES (?,?,?,?,?)",
                      ("coordinator", _ts(now()), "coordinator", code, n))
    codes = {r["code"] for r in alice.get("/api/v1/metrics/errors?codes=network").json()}
    assert codes == {"NET_PROBE_FAILED"}
