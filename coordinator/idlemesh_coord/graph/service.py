"""Record network facts from heartbeats and recompute clusters from the store (S5.4, S5.1).

`observe` runs on every heartbeat. It keeps each node's subnet in `node_networks` and writes the node's RTT
to this coordinator into `net_edges` only when something changed: the first figure, a new subnet, a large
change in RTT, or the slow refresh (`probes.rtt_refresh_h`). Most nodes are desktops that never move, so a
steady node costs about one row a day, not one per heartbeat.

The coordinator calls `recompute` on a timer (`clusters.recompute_every_s`). Measurements older than
`max_age_h` are ignored, so a node that stops reporting drifts back to a cluster of its own. Each change of
cluster or network is logged with the node key, so a cluster map can be explained afterwards.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta

from .. import obs
from ..store import Store, iso, now, parse
from ..telemetry import COORDINATOR_NODE as COORDINATOR  # the dst of an edge measured to the coordinator
from .network import ClusterPolicy, Link, Measurement, assign_ids, combine, components, joins, relayed

log = logging.getLogger("idlemesh_coord.graph")

HEARTBEAT = "heartbeat"  # net_edges.method for the RTT a node's heartbeats measure to this coordinator


@dataclass(frozen=True)
class RttRule:
    """When a heartbeat's RTT is worth a new row (site config `probes`)."""
    refresh_h: float = 24.0
    change_frac: float = 0.5   # a change of half the last figure,
    change_ms: float = 1.0     # and at least this much, counts as a change


def observe(store: Store, node: str, subnet: str | None, rtt_ms: float, rule: RttRule,
            org_id: str | None, site_id: str | None) -> dict:
    """Record what one heartbeat says about a node's network. Returns what was written."""
    out = {"network_changed": False, "rtt_recorded": False}
    at = now()
    with store.tx() as c:
        row = c.execute("SELECT subnet FROM node_networks WHERE node = ?", (node,)).fetchone()
        old = row["subnet"] if row else None
        if subnet and subnet != old:
            c.execute("INSERT INTO node_networks (node, subnet, since) VALUES (?,?,?) ON CONFLICT(node) DO UPDATE"
                      " SET subnet = excluded.subnet, since = excluded.since", (node, subnet, iso(at)))
            out["network_changed"] = True
        if rtt_ms > 0:  # 0 is the first heartbeat, before any round trip was timed
            last = c.execute("SELECT ts, rtt_ms_p50 FROM net_edges WHERE src = ? AND dst = ? AND method = ?"
                             " ORDER BY ts DESC LIMIT 1", (node, COORDINATOR, HEARTBEAT)).fetchone()
            due = (last is None or out["network_changed"]
                   or abs(rtt_ms - last["rtt_ms_p50"]) > max(rule.change_ms, rule.change_frac * last["rtt_ms_p50"])
                   or at - parse(last["ts"]) >= timedelta(hours=rule.refresh_h))
            if due:
                # Strictly after the last row: two heartbeats can share a clock tick (about 16 ms on Windows).
                ts = at if last is None else max(at, parse(last["ts"]) + timedelta(microseconds=1))
                out["rtt_recorded"] = c.execute(
                    "INSERT OR IGNORE INTO net_edges (src, dst, ts, method, rtt_ms_p50, rtt_ms_p95, org_id, site_id,"
                    " received_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (node, COORDINATOR, ts.isoformat(timespec="microseconds"), HEARTBEAT, rtt_ms, rtt_ms, org_id,
                     site_id, iso(at))).rowcount == 1
    if out["network_changed"]:
        obs.log(log, logging.INFO, "node changed network", node=node, from_subnet=old, to_subnet=subnet)
    return out


def measurements(store: Store, max_age_h: float) -> list[Measurement]:
    """Node-to-node measurements newer than `max_age_h`, oldest first."""
    since = iso(now() - timedelta(hours=max_age_h))
    rows = store.all(
        "SELECT src, dst, ts, rtt_ms_p50, rtt_ms_p95, bandwidth_mbps FROM net_edges"
        " WHERE ts >= ? AND src != ? AND dst != ? ORDER BY ts", (since, COORDINATOR, COORDINATOR))
    return [Measurement(r["src"], r["dst"], r["ts"], r["rtt_ms_p50"], r["rtt_ms_p95"], r["bandwidth_mbps"])
            for r in rows]


def coordinator_rtt(store: Store, max_age_h: float) -> dict[str, tuple[float, str]]:
    """Each node's newest RTT to this coordinator, newer than `max_age_h`: {node: (rtt_ms, ts)}."""
    since = iso(now() - timedelta(hours=max_age_h))
    out: dict[str, tuple[float, str]] = {}
    for r in store.all("SELECT src, ts, rtt_ms_p50 FROM net_edges WHERE dst = ? AND ts >= ?"
                       " AND rtt_ms_p50 IS NOT NULL ORDER BY ts", (COORDINATOR, since)):
        out[r["src"]] = (r["rtt_ms_p50"], r["ts"])
    return out


def subnets(store: Store) -> dict[str, str]:
    return {r["node"]: r["subnet"] for r in store.all("SELECT node, subnet FROM node_networks")}


def links(store: Store, wanted: set[str], max_age_h: float) -> tuple[list[Link], dict[str, str]]:
    """Measured links between `wanted` nodes, plus relayed estimates for same-subnet pairs with no direct
    measurement; and the nodes' subnets."""
    nets = {n: s for n, s in subnets(store).items() if n in wanted}
    direct = {k: v for k, v in combine(measurements(store, max_age_h)).items() if k[0] in wanted and k[1] in wanted}
    coord = {n: v for n, v in coordinator_rtt(store, max_age_h).items() if n in wanted}
    return list(direct.values()) + relayed(coord, nets, direct.keys()), nets


def recompute(store: Store, nodes: list[str], policy: ClusterPolicy, max_age_h: float) -> dict:
    """Cluster `nodes` (node keys of the enrolled, unrevoked devices) and store each node's cluster.
    Measurements naming a node outside `nodes` (a revoked device) are left out. Returns counts."""
    wanted = set(nodes)
    all_links, nets = links(store, wanted, max_age_h)
    clusters = components(wanted, all_links, policy, nets)
    previous = {r["node"]: r["cluster_id"] for r in store.all("SELECT node, cluster_id FROM node_clusters")}
    ids = assign_ids(clusters, previous)
    at = iso(now())
    changed = [n for n in ids if previous.get(n) != ids[n]]
    gone = [n for n in previous if n not in ids]
    with store.tx() as c:
        c.executemany("DELETE FROM node_clusters WHERE node = ?", [(n,) for n in gone])
        c.executemany("INSERT INTO node_clusters (node, cluster_id, since) VALUES (?,?,?)"
                      " ON CONFLICT(node) DO UPDATE SET cluster_id = excluded.cluster_id, since = excluded.since",
                      [(n, ids[n], at) for n in changed])
    for n in changed:
        obs.log(log, logging.INFO, "node changed cluster", node=n, from_cluster=previous.get(n),
                to_cluster=ids[n])
    summary = {"nodes": len(ids), "clusters": len(clusters), "links": len(all_links), "changed": len(changed),
               "removed": len(gone)}
    if changed or gone:
        obs.log(log, logging.INFO, "clusters recomputed", **summary)
    return summary


def network_view(store: Store, devices: dict[str, dict], policy: ClusterPolicy, max_age_h: float) -> dict:
    """The graph as an administrator sees it: `devices` maps node keys to {"device_id", "name"}. Links and
    clusters are limited to those devices; node keys never leave the coordinator."""
    all_links, nets = links(store, set(devices), max_age_h)
    coord = coordinator_rtt(store, max_age_h)
    member = {r["node"]: (r["cluster_id"], r["since"]) for r in store.all("SELECT * FROM node_clusters")}
    clusters: dict[str, list[str]] = {}
    nodes = []
    for key, d in sorted(devices.items(), key=lambda kv: kv[1]["name"]):
        cid, since = member.get(key, (None, None))
        rtt = coord.get(key)
        nodes.append({**d, "subnet": nets.get(key), "coordinator_rtt_ms": rtt[0] if rtt else None,
                      "cluster_id": cid, "cluster_since": since})
        if cid:
            clusters.setdefault(cid, []).append(d["device_id"])
    return {
        "policy": {"max_rtt_ms": policy.max_rtt_ms, "min_mbps": policy.min_mbps, "max_age_h": max_age_h},
        "nodes": nodes,
        "links": [_link_out(link, devices, policy, nets) for link in all_links],
        "clusters": [{"cluster_id": cid, "devices": sorted(ids)} for cid, ids in
                     sorted(clusters.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
    }


def _link_out(link: Link, devices: dict[str, dict], policy: ClusterPolicy, nets: dict[str, str]) -> dict:
    return {"a": devices[link.a]["device_id"], "b": devices[link.b]["device_id"], "source": link.source,
            "rtt_ms_p50": link.rtt_ms_p50, "rtt_ms_p95": link.rtt_ms_p95, "bandwidth_mbps": link.bandwidth_mbps,
            "measured_at": link.measured_at, "joins": joins(link, policy, nets)}
