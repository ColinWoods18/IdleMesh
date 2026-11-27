"""Each node's link to the coordinator (S5.2, S5.3, S5.5): when a bandwidth test is due, how it is stored, when a
link counts as degraded, and how placement uses the figure."""
import logging
from datetime import datetime, timedelta

from idlemesh_coord import graph
from idlemesh_coord.catalog import CATALOG
from idlemesh_coord.domain.placement import (LLAMACPP_RPC_V090, DeviceProfile, PlacementPolicy, plan,
                                             reestimate_with_path_rtt, stage_network_ms)
from idlemesh_coord.graph import links
from idlemesh_coord.store import Store, iso, now

GB = 10**9
BW = {"mbps": 90.0, "up_mbps": 95.0, "down_mbps": 90.0, "bytes": 8 << 20, "duration_ms": 1500.0}
RULE = links.BandwidthRule(every_h=168, off_hours_only=True, off_hours_start="20:00", off_hours_end="07:00")
WEDNESDAY_NOON = datetime(2026, 11, 25, 12, 0)
WEDNESDAY_NIGHT = datetime(2026, 11, 25, 23, 30)


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "db.sqlite3"))


def _row(store, node, mbps, hours_ago=0.0, method=links.TUNNEL, rtt=None):
    with store.tx() as c:
        c.execute("INSERT INTO net_edges (src, dst, ts, method, rtt_ms_p50, rtt_ms_p95, bandwidth_mbps, received_at)"
                  " VALUES (?,?,?,?,?,?,?,?)", (node, "coordinator", iso(now() - timedelta(hours=hours_ago)), method,
                                                rtt, rtt, mbps, iso(now())))


def test_off_hours_cross_midnight_and_include_weekends():
    assert links.in_off_hours(WEDNESDAY_NIGHT, "20:00", "07:00")
    assert links.in_off_hours(datetime(2026, 11, 25, 6, 59), "20:00", "07:00")
    assert not links.in_off_hours(datetime(2026, 11, 25, 7, 0), "20:00", "07:00")
    assert not links.in_off_hours(WEDNESDAY_NOON, "20:00", "07:00")
    assert links.in_off_hours(datetime(2026, 11, 28, 12, 0), "20:00", "07:00")  # Saturday
    assert links.in_off_hours(WEDNESDAY_NOON, "11:00", "13:00") and not links.in_off_hours(WEDNESDAY_NIGHT, "11:00", "13:00")


def test_first_test_and_network_change_are_due_at_once_the_refresh_waits_for_off_hours(tmp_path):
    store = _store(tmp_path)
    assert links.due(store, "a", RULE, WEDNESDAY_NOON) == "first"
    _row(store, "a", 90, hours_ago=1)
    assert links.due(store, "a", RULE, WEDNESDAY_NOON) is None
    with store.tx() as c:  # the node moved network after its last test
        c.execute("INSERT INTO node_networks (node, subnet, since) VALUES ('a', '10.2.0.0/24', ?)", (iso(now()),))
    assert links.due(store, "a", RULE, WEDNESDAY_NOON) == "network_changed"

    _row(store, "b", 90, hours_ago=200)  # older than bandwidth_every_h
    assert links.due(store, "b", RULE, WEDNESDAY_NOON) is None
    assert links.due(store, "b", RULE, WEDNESDAY_NIGHT) == "refresh"
    anytime = links.BandwidthRule(every_h=168, off_hours_only=False)
    assert links.due(store, "b", anytime, WEDNESDAY_NOON) == "refresh"


def test_a_test_is_stored_and_a_much_slower_link_is_a_degradation_event(tmp_path, caplog):
    store = _store(tmp_path)
    first = links.record(store, "a", BW, None, "org", "site", "first")
    assert first == {"node": "a", "mbps": 90.0, "reason": "first", "degraded": False, "previous_mbps": None}
    row = store.one("SELECT * FROM net_edges WHERE src = 'a'")
    assert (row["dst"], row["method"], row["bandwidth_mbps"], row["org_id"]) == ("coordinator", "tunnel_test", 90.0, "org")

    with caplog.at_level(logging.WARNING, logger="idlemesh_coord.graph"):
        assert not links.record(store, "a", {**BW, "mbps": 60.0}, None, None, None, "refresh")["degraded"]
        assert not caplog.records
        out = links.record(store, "a", {**BW, "mbps": 25.0}, None, None, None, "refresh")
    assert out["degraded"] and out["previous_mbps"] == 60.0
    assert [r.error_code for r in caplog.records] == ["LINK_DEGRADED"]
    assert links.link_mbps(store, 24)["a"][0] == 25.0  # the newest figure wins


def test_old_figures_do_not_count_for_placement(tmp_path):
    store = _store(tmp_path)
    _row(store, "a", 90, hours_ago=600)
    _row(store, "b", 40, hours_ago=1)
    assert set(links.link_mbps(store, 3 * 168)) == {"b"}


def test_a_heartbeat_rtt_jump_on_the_same_network_is_a_degradation_event(tmp_path, caplog):
    store = _store(tmp_path)
    rule = graph.RttRule(refresh_h=24, change_frac=0.5, change_ms=1.0)
    graph.observe(store, "a", "10.1.0.0/24", 2.0, rule, None, None)
    with caplog.at_level(logging.WARNING, logger="idlemesh_coord.graph"):
        assert "degraded" not in graph.observe(store, "a", "10.1.0.0/24", 1.0, rule, None, None)  # faster: fine
        out = graph.observe(store, "a", "10.1.0.0/24", 9.0, rule, None, None)
    assert out["degraded"] == {"previous": 2.0, "current": 9.0}  # 1.0 was too small a change to record
    assert [r.error_code for r in caplog.records] == ["LINK_DEGRADED"]
    with caplog.at_level(logging.WARNING, logger="idlemesh_coord.graph"):
        caplog.clear()
        assert "degraded" not in graph.observe(store, "a", "10.9.0.0/16", 30.0, rule, None, None)  # new network
    assert not caplog.records


def test_tunnel_test_rows_do_not_change_relayed_links(tmp_path):
    store = _store(tmp_path)
    _row(store, "a", None, method="heartbeat", rtt=2.0, hours_ago=1)
    _row(store, "a", 90, rtt=0.3)
    from idlemesh_coord.graph.service import coordinator_rtt
    assert coordinator_rtt(store, 72)["a"][0] == 2.0


# --- Placement with link bandwidth (S5.3) ---------------------------------------------------------------
def dev(i, budget_gb, bw, rtt, **kw):
    return DeviceProfile(device_id=i, name=i, os="t", arch="t", backend_digest="fake-runtime-0.1", formats=("gguf",),
                         budget_bytes=int(budget_gb * GB), mem_bandwidth_gbps=bw, rtt_ms=rtt, **kw)


POLICY = PlacementPolicy(backend_digest="fake-runtime-0.1", require_all=True, local_device="mac")


def test_a_measured_link_adds_its_traffic_to_the_stage_and_lowers_its_score():
    cost = LLAMACPP_RPC_V090
    assert stage_network_ms(5, None, cost) == 5 * cost.round_trips_per_token
    assert round(stage_network_ms(5, 100, cost), 2) == round(5 * 1.02 + 0.62e6 * 8 / 100e6 * 1000, 2)  # 54.7 ms

    unknown = plan([dev("mac", 64, 800, 1), dev("hp", 8, 51, 5)], CATALOG["fixture-small"], POLICY)
    slow = plan([dev("mac", 64, 800, 1), dev("hp", 8, 51, 5, link_mbps=100, link_source="tunnel_test")],
                CATALOG["fixture-small"], POLICY)
    hp_before = next(s for s in unknown["stages"] if s["device_id"] == "hp")
    hp_after = next(s for s in slow["stages"] if s["device_id"] == "hp")
    assert (hp_after["link_mbps"], hp_after["link_source"]) == (100, "tunnel_test")
    assert hp_after["est_network_ms"] - hp_before["est_network_ms"] == round(0.62e6 * 8 / 100e6 * 1000, 2)
    assert unknown["latency_budget"]["links_unmeasured"] == ["hp"] and slow["latency_budget"]["links_unmeasured"] == []
    score = {s["device_id"]: s["components"]["latency"] for s in slow["scores"]}
    assert score["hp"] < {s["device_id"]: s["components"]["latency"] for s in unknown["scores"]}["hp"]


def test_a_link_timed_at_load_replaces_the_planned_figure():
    out = plan([dev("mac", 64, 800, 1), dev("hp", 8, 51, 5)], CATALOG["fixture-small"], POLICY)
    before = out["latency_budget"]["per_token_ms"]
    reestimate_with_path_rtt(out, {}, links={"hp": {**BW, "mbps": 200.0}})
    hp = next(s for s in out["stages"] if s["device_id"] == "hp")
    assert (hp["link_mbps"], hp["link_source"], hp["planned_link_mbps"]) == (200.0, "tunnel_test", None)
    assert out["planned_latency_budget"]["per_token_ms"] == before
    assert round(out["latency_budget"]["per_token_ms"] - before, 2) == round(0.62e6 * 8 / 200e6 * 1000, 2)
