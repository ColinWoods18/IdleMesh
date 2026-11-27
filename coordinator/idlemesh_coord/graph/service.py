"""Recompute clusters from stored measurements and record the result (S5.4).

The coordinator calls `recompute` on a timer (`clusters.recompute_every_s`). Measurements older than
`max_age_h` are ignored, so a node that stops probing drifts back to a cluster of its own. Each change of
cluster is logged with the node key and both cluster IDs, so a cluster map can be explained afterwards.
"""
import logging
from datetime import timedelta

from .. import obs
from ..store import Store, iso, now
from ..telemetry import COORDINATOR_NODE as COORDINATOR  # the dst of an edge measured to the coordinator
from .network import ClusterPolicy, Link, Measurement, assign_ids, close_enough, combine, components

log = logging.getLogger("idlemesh_coord.graph")


def measurements(store: Store, max_age_h: float) -> list[Measurement]:
    """Node-to-node measurements newer than `max_age_h`, oldest first."""
    since = iso(now() - timedelta(hours=max_age_h))
    rows = store.all(
        "SELECT src, dst, ts, rtt_ms_p50, rtt_ms_p95, bandwidth_mbps FROM net_edges"
        " WHERE ts >= ? AND src != ? AND dst != ? ORDER BY ts", (since, COORDINATOR, COORDINATOR))
    return [Measurement(r["src"], r["dst"], r["ts"], r["rtt_ms_p50"], r["rtt_ms_p95"], r["bandwidth_mbps"])
            for r in rows]


def recompute(store: Store, nodes: list[str], policy: ClusterPolicy, max_age_h: float) -> dict:
    """Cluster `nodes` (node keys of the enrolled, unrevoked devices) and store each node's cluster.
    Measurements naming a node outside `nodes` (a revoked device) are left out. Returns counts."""
    wanted = set(nodes)
    links = [link for link in combine(measurements(store, max_age_h)).values()
             if link.a in wanted and link.b in wanted]
    clusters = components(wanted, links, policy)
    previous = {r["node"]: r["cluster_id"] for r in store.all("SELECT node, cluster_id FROM node_clusters")}
    ids = assign_ids(clusters, previous)
    at = iso(now())
    changed = [n for n in ids if previous.get(n) != ids[n]]
    with store.tx() as c:
        gone = [n for n in previous if n not in ids]
        c.executemany("DELETE FROM node_clusters WHERE node = ?", [(n,) for n in gone])
        c.executemany("INSERT INTO node_clusters (node, cluster_id, since) VALUES (?,?,?)"
                      " ON CONFLICT(node) DO UPDATE SET cluster_id = excluded.cluster_id, since = excluded.since",
                      [(n, ids[n], at) for n in changed])
    for n in changed:
        obs.log(log, logging.INFO, "node changed cluster", node=n, from_cluster=previous.get(n),
                to_cluster=ids[n])
    summary = {"nodes": len(ids), "clusters": len(clusters), "links": len(links), "changed": len(changed),
               "removed": len(gone)}
    if changed or gone:
        obs.log(log, logging.INFO, "clusters recomputed", **summary)
    return summary


def network_view(store: Store, devices: dict[str, dict], policy: ClusterPolicy, max_age_h: float) -> dict:
    """The graph as an administrator sees it: `devices` maps node keys to {"device_id", "name"}. Links and
    clusters are limited to those devices; node keys never leave the coordinator."""
    links = [link for link in combine(measurements(store, max_age_h)).values()
             if link.a in devices and link.b in devices]
    member = {r["node"]: (r["cluster_id"], r["since"]) for r in store.all("SELECT * FROM node_clusters")}
    clusters: dict[str, list[str]] = {}
    nodes = []
    for key, d in sorted(devices.items(), key=lambda kv: kv[1]["name"]):
        cid, since = member.get(key, (None, None))
        nodes.append({**d, "cluster_id": cid, "cluster_since": since})
        if cid:
            clusters.setdefault(cid, []).append(d["device_id"])
    return {
        "policy": {"max_rtt_ms": policy.max_rtt_ms, "min_mbps": policy.min_mbps, "max_age_h": max_age_h},
        "nodes": nodes,
        "links": [_link_out(link, devices, policy) for link in links],
        "clusters": [{"cluster_id": cid, "devices": sorted(ids)} for cid, ids in
                     sorted(clusters.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
    }


def _link_out(link: Link, devices: dict[str, dict], policy: ClusterPolicy) -> dict:
    return {"a": devices[link.a]["device_id"], "b": devices[link.b]["device_id"], "rtt_ms_p50": link.rtt_ms_p50,
            "rtt_ms_p95": link.rtt_ms_p95, "bandwidth_mbps": link.bandwidth_mbps, "measured_at": link.measured_at,
            "joins": close_enough(link, policy)}
