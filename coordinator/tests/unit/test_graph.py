"""Clusterer v0: links from measurements, the threshold rule, components and stable IDs (S5.4)."""
from datetime import timedelta

from idlemesh_coord import graph
from idlemesh_coord.graph import ClusterPolicy, Link, Measurement, assign_ids, close_enough, combine, components
from idlemesh_coord.store import Store, iso, now

POLICY = ClusterPolicy(max_rtt_ms=5.0, min_mbps=100.0)


def m(src, dst, rtt=None, bw=None, ts="2026-11-27T10:00:00+00:00"):
    return Measurement(src, dst, ts, rtt, None if rtt is None else rtt * 1.5, bw)


def link(a, b, rtt, bw=None):
    return Link(a, b, rtt, rtt, bw, "t")


def test_both_directions_combine_to_the_worse_value():
    links = combine([m("b", "a", rtt=1.0, bw=900), m("a", "b", rtt=3.0, bw=400)])
    assert list(links) == [("a", "b")]
    ab = links[("a", "b")]
    assert (ab.rtt_ms_p50, ab.bandwidth_mbps) == (3.0, 400)


def test_the_newest_value_of_each_field_wins_and_older_fields_are_kept():
    links = combine([m("a", "b", rtt=9.0, ts="2026-11-27T09:00:00+00:00"),
                     m("a", "b", bw=500, ts="2026-11-27T09:30:00+00:00"),
                     m("a", "b", rtt=2.0, ts="2026-11-27T10:00:00+00:00")])
    ab = links[("a", "b")]
    assert (ab.rtt_ms_p50, ab.bandwidth_mbps, ab.measured_at) == (2.0, 500, "2026-11-27T10:00:00+00:00")


def test_self_measurements_are_ignored():
    assert combine([m("a", "a", rtt=0.1)]) == {}


def test_the_threshold_needs_a_known_low_rtt_and_enough_bandwidth_when_known():
    assert close_enough(link("a", "b", 4.9), POLICY)
    assert close_enough(link("a", "b", 5.0, bw=100), POLICY)
    assert not close_enough(link("a", "b", 5.1), POLICY)
    assert not close_enough(link("a", "b", 1.0, bw=50), POLICY)
    assert not close_enough(Link("a", "b", None, None, 900, "t"), POLICY)  # bandwidth alone never joins


def test_components_join_through_a_third_node_and_keep_lonely_nodes_apart():
    links = [link("a", "b", 1), link("b", "c", 2), link("c", "d", 40), link("e", "f", 0.5)]
    got = components(["a", "b", "c", "d", "e", "f", "g"], links, POLICY)
    assert got == [frozenset("abc"), frozenset("ef"), frozenset("d"), frozenset("g")]


def test_a_slow_link_alone_does_not_join():
    assert components(["x", "y"], [link("x", "y", 1, bw=10)], POLICY) == [frozenset("x"), frozenset("y")]


def test_cluster_ids_survive_a_node_joining_or_leaving():
    first = assign_ids([frozenset("abc"), frozenset("de")], {})
    assert first["a"] == first["b"] == first["c"] != first["d"] == first["e"]
    # f joins the first cluster, c leaves for one of its own.
    second = assign_ids([frozenset("abf"), frozenset("de"), frozenset("c")], first)
    assert second["a"] == second["f"] == first["a"]
    assert second["d"] == first["d"]
    assert second["c"] not in {first["a"], first["d"]}


def test_when_two_clusters_merge_the_bigger_one_keeps_its_id_and_ids_stay_unique():
    first = assign_ids([frozenset("abc"), frozenset("de")], {})
    merged = assign_ids([frozenset("abcde")], first)
    assert set(merged.values()) == {first["a"]}
    # Split again: the larger part keeps the ID, the other gets a fresh one rather than a duplicate.
    split = assign_ids([frozenset("abc"), frozenset("de")], merged)
    assert split["a"] == first["a"] and split["d"] != split["a"]


def test_new_ids_are_deterministic():
    assert assign_ids([frozenset("ab")], {}) == assign_ids([frozenset("ab")], {})


def _edge(store, src, dst, rtt, hours_ago=0.0, bw=None):
    with store.tx() as c:
        c.execute("INSERT INTO net_edges (src, dst, ts, method, rtt_ms_p50, rtt_ms_p95, bandwidth_mbps, received_at)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (src, dst, iso(now() - timedelta(hours=hours_ago)), "tunnel", rtt, rtt, bw, iso(now())))


def _subnet(store, node, subnet):
    with store.tx() as c:
        c.execute("INSERT INTO node_networks (node, subnet, since) VALUES (?,?,?)", (node, subnet, iso(now())))


def test_recompute_stores_clusters_ignores_stale_coordinator_and_unknown_edges(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    for n in "abc":
        _subnet(store, n, "10.1.0.0/24")
    _edge(store, "a", "b", 1.0)
    _edge(store, "b", "c", 2.0, hours_ago=100)      # too old
    _edge(store, "c", "coordinator", 9.0)           # c is slow to the coordinator: no relayed link joins it
    _edge(store, "c", "gone", 0.3)                  # a node that is no longer enrolled
    out = graph.recompute(store, ["a", "b", "c"], POLICY, max_age_h=48)
    assert out == {"nodes": 3, "clusters": 2, "links": 1, "changed": 3, "removed": 0}
    rows = {r["node"]: r["cluster_id"] for r in store.all("SELECT * FROM node_clusters")}
    assert rows["a"] == rows["b"] != rows["c"]
    # Nothing changed: nothing is rewritten. A node that leaves the fleet loses its row.
    assert graph.recompute(store, ["a", "b", "c"], POLICY, 48)["changed"] == 0
    out = graph.recompute(store, ["a", "b"], POLICY, 48)
    assert out["removed"] == 1 and {r["node"] for r in store.all("SELECT node FROM node_clusters")} == {"a", "b"}


def test_the_graph_keeps_every_measured_link_and_marks_which_ones_join():
    from idlemesh_coord.graph import build
    g = build(["a", "b", "c"], [link("a", "b", 1), link("b", "c", 40)], POLICY)
    assert set(g.nodes) == {"a", "b", "c"}
    assert g.edges["a", "b"]["joins"] is True and g.edges["b", "c"]["joins"] is False
    assert g.edges["b", "c"]["rtt_ms_p50"] == 40


NETS = {"a": "10.1.0.0/24", "b": "10.1.0.0/24", "c": "10.1.0.0/24", "x": "10.2.0.0/24"}


def test_relayed_links_cover_same_subnet_pairs_without_a_direct_measurement():
    coord = {"a": (0.4, "t1"), "b": (0.5, "t2"), "c": (0.3, "t3"), "x": (0.2, "t4"), "nosubnet": (0.1, "t5")}
    got = {(link.a, link.b): link for link in graph.relayed(coord, NETS, measured=[("a", "c")])}
    assert set(got) == {("a", "b"), ("b", "c")}  # a-c has a real figure; x and nosubnet have no same-subnet peer
    ab = got[("a", "b")]
    assert (ab.rtt_ms_p50, ab.source, ab.measured_at) == (0.9, "relayed", "t2")


def test_clusters_never_span_subnets_and_need_a_known_subnet():
    links = [link("a", "b", 1), link("b", "x", 0.5), link("c", "n", 0.5)]
    got = components(["a", "b", "c", "x", "n"], links, POLICY, NETS)
    assert got == [frozenset("ab"), frozenset("c"), frozenset("n"), frozenset("x")]


def test_relayed_estimates_cluster_a_subnet_and_a_slow_node_stays_out(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    for n, net in NETS.items():
        _subnet(store, n, net)
    for n, rtt in (("a", 0.6), ("b", 0.8), ("c", 7.0), ("x", 0.4)):
        _edge(store, n, "coordinator", rtt)
    assert graph.recompute(store, list(NETS), POLICY, 48)["clusters"] == 3  # {a, b}, {c}, {x}
    rows = {r["node"]: r["cluster_id"] for r in store.all("SELECT * FROM node_clusters")}
    assert rows["a"] == rows["b"] and len({rows["a"], rows["c"], rows["x"]}) == 3


def test_heartbeats_record_rtt_only_when_something_changed(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    rule = graph.RttRule(refresh_h=24, change_frac=0.5, change_ms=1.0)

    def beat(subnet, rtt):
        return graph.observe(store, "a", subnet, rtt, rule, None, None)

    assert beat("10.1.0.0/24", 0.0) == {"network_changed": True, "rtt_recorded": False}  # no round trip yet
    assert beat("10.1.0.0/24", 2.0)["rtt_recorded"]          # the first figure
    assert not beat("10.1.0.0/24", 2.8)["rtt_recorded"]      # within max(1 ms, 50%)
    assert beat("10.1.0.0/24", 3.5)["rtt_recorded"]          # 1.5 ms more than the last row: a change
    out = beat("10.9.0.0/16", 3.5)                           # moved to another network: recorded at once
    assert out == {"network_changed": True, "rtt_recorded": True}
    assert not beat(None, 3.5)["network_changed"]            # an unknown subnet keeps the last known one
    assert store.one("SELECT subnet FROM node_networks WHERE node = 'a'")["subnet"] == "10.9.0.0/16"
    # After the refresh interval a steady figure is recorded again.
    with store.tx() as c:
        for i, r in enumerate(c.execute("SELECT rowid FROM net_edges").fetchall()):
            c.execute("UPDATE net_edges SET ts = ? WHERE rowid = ?", (iso(now() - timedelta(hours=26 - i)), r[0]))
    assert beat("10.9.0.0/16", 3.5)["rtt_recorded"]
    assert len(store.all("SELECT * FROM net_edges")) == 4
