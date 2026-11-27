"""Network measurements in, clusters out, through the coordinator API (S5.4)."""
from datetime import datetime, timedelta, timezone

from conftest import GB

from idlemesh_coord.api.app import recompute_clusters

CAPS = {"os": "windows", "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
        "allocatable_bytes": 4 * GB, "mem_bandwidth_gbps": 50, "rtt_ms": 2, "power": "ac"}
CONSENT = {"idle_data": True, "diagnostics": True, "policy_version": "v0", "recorded_at": "2026-11-19T00:00:00Z"}


def _ts(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")


def _device(client, user, name, consent=CONSENT) -> dict:
    code = user.post("/api/v1/devices/enrollment-codes").json()["code"]
    r = client.post("/api/v1/devices/enroll", json={"code": code, "name": name, "public_key": "k", "capabilities": CAPS,
                                                    "policy": {"memory_cap_bytes": 4 * GB}, "consent": consent})
    assert r.status_code == 201, r.text
    return r.json()


def _edge(dst, rtt=None, bw=None, minutes_ago=0, method="tunnel") -> dict:
    rec = {"kind": "net_edge", "ts": _ts(minutes_ago), "dst": dst, "method": method}
    if rtt is not None:
        rec |= {"rtt_ms_p50": rtt, "rtt_ms_p95": rtt * 2, "samples": 20}
    if bw is not None:
        rec["bandwidth_mbps"] = bw
    return rec


def _beat(client, dev, subnet=None, rtt=1.0, consent=CONSENT):
    caps = {**CAPS, "rtt_ms": rtt, **({"network": {"subnet": subnet}} if subnet else {})}
    return client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat", json={"capabilities": caps, "consent": consent},
                       headers={"Authorization": f"Bearer {dev['device_token']}"})


def _upload(client, dev, records):
    return client.post(f"/api/v1/devices/{dev['device_id']}/telemetry",
                       json={"schema_version": 1, "records": records},
                       headers={"Authorization": f"Bearer {dev['device_token']}"})


def test_measured_links_become_clusters_an_admin_can_see(client, group, app):
    alice, gid = group["alice"], group["group"]["group_id"]
    a, b, c = (_device(client, alice, n) for n in ("Lab A", "Lab B", "Other building"))
    for d in (a, b, c):  # all on one subnet; slow to the coordinator, so only measured links can join
        assert _beat(client, d, "10.1.0.0/24", rtt=40.0).status_code == 200
    assert _upload(client, a, [_edge(b["device_id"], rtt=0.8), _edge("coordinator", rtt=0.4)]).status_code == 200
    assert _upload(client, b, [_edge(a["device_id"], rtt=1.1, bw=940), _edge(c["device_id"], rtt=18.0)]).json() \
        == {"accepted": 2, "duplicates": 0}

    assert recompute_clusters(app)["clusters"] == 2
    view = alice.get(f"/api/v1/groups/{gid}/network").json()
    assert view["policy"]["max_rtt_ms"] == 5.0
    by_id = {n["device_id"]: n for n in view["nodes"]}
    assert by_id[a["device_id"]]["cluster_id"] == by_id[b["device_id"]]["cluster_id"]
    assert by_id[c["device_id"]]["cluster_id"] != by_id[a["device_id"]]["cluster_id"]
    assert view["clusters"][0]["devices"] == sorted([a["device_id"], b["device_id"]])
    links = {frozenset((x["a"], x["b"])): x for x in view["links"]}
    ab = links[frozenset((a["device_id"], b["device_id"]))]
    assert (ab["rtt_ms_p50"], ab["bandwidth_mbps"], ab["joins"]) == (1.1, 940, True)
    assert links[frozenset((b["device_id"], c["device_id"]))]["joins"] is False
    # Node keys stay on the coordinator: the view names devices only.
    assert not any(k.startswith("n_") for n in view["nodes"] for k in n.values() if isinstance(k, str))


def test_edges_are_stored_under_pseudonymous_keys_and_retries_are_ignored(client, group, app):
    alice = group["alice"]
    a, b = _device(client, alice, "A"), _device(client, alice, "B")
    recs = [_edge(b["device_id"], rtt=1.0)]
    assert _upload(client, a, recs).json() == {"accepted": 1, "duplicates": 0}
    assert _upload(client, a, recs).json() == {"accepted": 0, "duplicates": 1}
    row = app.state.store.one("SELECT src, dst FROM net_edges")
    keys = app.state.node_keys
    assert (row["src"], row["dst"]) == (keys.key(a["device_id"]), keys.key(b["device_id"]))


def test_network_measurements_need_diagnostics_consent(client, group):
    alice = group["alice"]
    dev = _device(client, alice, "A", consent={**CONSENT, "diagnostics": False})
    r = _upload(client, dev, [_edge("coordinator", rtt=1.0)])
    assert r.status_code == 403 and r.json()["error"]["code"] == "TELEMETRY_NO_CONSENT"


def test_invalid_net_edge_records_are_refused(client, group):
    dev = _device(client, group["alice"], "A")
    assert _upload(client, dev, [{"kind": "net_edge", "ts": _ts(), "dst": "coordinator", "method": "tunnel"}]) \
        .status_code == 400  # no measurement at all
    assert _upload(client, dev, [{**_edge("coordinator", rtt=5.0), "rtt_ms_p95": 1.0}]).status_code == 400
    assert _upload(client, dev, [_edge("../etc", rtt=1.0)]).status_code == 400
    assert _upload(client, dev, [_edge("coordinator", rtt=1.0, method="ping")]).status_code == 400


def test_only_group_admins_see_the_network(client, group):
    gid = group["group"]["group_id"]
    assert group["bob"].get(f"/api/v1/groups/{gid}/network").status_code == 403
    assert group["alice"].get(f"/api/v1/groups/{gid}/network").status_code == 200


def test_heartbeats_give_each_subnet_its_own_cluster(client, group, app):
    alice, gid = group["alice"], group["group"]["group_id"]
    devs = {n: _device(client, alice, n) for n in ("Lab 1", "Lab 2", "Lab 3", "Library 1", "Library 2")}
    for n, d in devs.items():
        subnet = "10.1.0.0/24" if n.startswith("Lab") else "10.2.0.0/24"
        assert _beat(client, d, subnet, rtt=0.6).status_code == 200
        assert _beat(client, d, subnet, rtt=0.7).status_code == 200  # steady: no second row
    assert len(app.state.store.all("SELECT * FROM net_edges")) == 5
    assert recompute_clusters(app)["clusters"] == 2
    view = alice.get(f"/api/v1/groups/{gid}/network").json()
    assert [len(c["devices"]) for c in view["clusters"]] == [3, 2]
    node = next(n for n in view["nodes"] if n["name"] == "Lab 1")
    assert (node["subnet"], node["coordinator_rtt_ms"]) == ("10.1.0.0/24", 0.6)
    assert {lk["source"] for lk in view["links"]} == {"relayed"} and all(lk["joins"] for lk in view["links"])


def test_a_subnet_must_be_a_network_address(client, group):
    dev = _device(client, group["alice"], "A")
    for bad in ("10.1.0.7/24", "10.1.0.0", "not-a-subnet", "10.1.0.0/33"):
        r = _beat(client, dev, bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_FIELDS", bad
    assert _beat(client, dev, "fd00:1::/64").status_code == 200


def test_without_diagnostics_consent_heartbeats_record_no_network_figures(client, group, app):
    no = {**CONSENT, "diagnostics": False}
    dev = _device(client, group["alice"], "A", consent=no)
    assert _beat(client, dev, "10.1.0.0/24", consent=no).status_code == 200
    assert app.state.store.all("SELECT * FROM net_edges") == []
    assert app.state.store.all("SELECT * FROM node_networks") == []
