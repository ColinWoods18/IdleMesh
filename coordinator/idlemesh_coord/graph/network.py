"""Clusterer v0 (S5.4, S5.1): a threshold graph on RTT and bandwidth, split into connected components.

A cluster never spans networks (D-24): two nodes can join only when both report the same subnet. Within a
subnet they join when the path between them is fast enough to split a model across (`ClusterPolicy`). A
cluster is a connected component of those links, so two nodes can share a cluster through a third. A node
with no fast link, or no known subnet, is a cluster of its own.

A link is either measured node to node (peer probes, later) or relayed: estimated from each node's RTT to
its own network's coordinator (S5.1). The relayed figure is an upper bound on the direct path, and it is
used only when the pair has no direct measurement.

The graph is a networkx graph (PROJECT_PLAN), so a later clusterer can swap connected components for
community detection without changing how the graph is built.
"""
import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

import networkx as nx


@dataclass(frozen=True)
class Measurement:
    """One directed measurement from `src` to `dst` (a NetEdge row). Either part may be missing: the RTT
    probe and the bandwidth test run on different schedules (S5.1, S5.2)."""
    src: str
    dst: str
    ts: str
    rtt_ms_p50: float | None = None
    rtt_ms_p95: float | None = None
    bandwidth_mbps: float | None = None


@dataclass(frozen=True)
class Link:
    """Both directions between two nodes, combined. `a` sorts before `b`."""
    a: str
    b: str
    rtt_ms_p50: float | None
    rtt_ms_p95: float | None
    bandwidth_mbps: float | None
    measured_at: str
    source: str = "measured"  # or "relayed": the sum of both nodes' RTT to the coordinator


@dataclass(frozen=True)
class ClusterPolicy:
    """When two nodes are close enough to share a cluster. Defaults are a first guess for a campus LAN
    (D-22): a few milliseconds keeps one building together and splits most cross-building paths."""
    max_rtt_ms: float = 5.0
    min_mbps: float = 100.0   # applies only once the bandwidth is known (S5.2)


def combine(measurements: Iterable[Measurement]) -> dict[tuple[str, str], Link]:
    """Links keyed by the sorted node pair. For each direction the newest value of each field is used; the
    two directions are then combined pessimistically (the higher RTT, the lower bandwidth), because a split
    model sends traffic both ways."""
    latest: dict[tuple[str, str], dict] = {}
    for m in sorted(measurements, key=lambda m: m.ts):
        if m.src == m.dst:
            continue
        d = latest.setdefault((m.src, m.dst), {"ts": m.ts})
        d["ts"] = m.ts
        for f in ("rtt_ms_p50", "rtt_ms_p95", "bandwidth_mbps"):
            v = getattr(m, f)
            if v is not None:
                d[f] = v
    links: dict[tuple[str, str], Link] = {}
    for (src, dst) in latest:
        a, b = sorted((src, dst))
        if (a, b) in links:
            continue
        sides = [s for s in (latest.get((a, b)), latest.get((b, a))) if s]
        links[(a, b)] = Link(a, b, _worst(sides, "rtt_ms_p50", max), _worst(sides, "rtt_ms_p95", max),
                             _worst(sides, "bandwidth_mbps", min), max(s["ts"] for s in sides))
    return links


def close_enough(link: Link, policy: ClusterPolicy) -> bool:
    """A link joins two nodes when its RTT is known and low enough, and its bandwidth, if known, is high
    enough. A link with only a bandwidth figure does not join nodes: RTT is the measurement every link has."""
    if link.rtt_ms_p50 is None or link.rtt_ms_p50 > policy.max_rtt_ms:
        return False
    return link.bandwidth_mbps is None or link.bandwidth_mbps >= policy.min_mbps


def relayed(coordinator_rtt: dict[str, tuple[float, str]], subnets: dict[str, str],
            measured: Iterable[tuple[str, str]] = ()) -> list[Link]:
    """Estimated links between nodes of the same subnet, from each node's (rtt_ms, ts) to the coordinator.
    Pairs in `measured` (sorted pairs with a direct measurement) are skipped: a real figure always wins."""
    skip = set(measured)
    by_subnet: dict[str, list[str]] = {}
    for n in sorted(coordinator_rtt):
        if n in subnets:
            by_subnet.setdefault(subnets[n], []).append(n)
    out = []
    for members in by_subnet.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if (a, b) in skip:
                    continue
                (ra, ta), (rb, tb) = coordinator_rtt[a], coordinator_rtt[b]
                rtt = round(ra + rb, 3)
                out.append(Link(a, b, rtt, rtt, None, max(ta, tb), source="relayed"))
    return out


def joins(link: Link, policy: ClusterPolicy, subnets: dict[str, str] | None = None) -> bool:
    """Whether a link joins its two nodes: it passes `policy` and, when `subnets` is given, both nodes report
    the same subnet (D-24)."""
    if subnets is not None and (subnets.get(link.a) is None or subnets.get(link.a) != subnets.get(link.b)):
        return False
    return close_enough(link, policy)


def build(nodes: Iterable[str], links: Iterable[Link], policy: ClusterPolicy,
          subnets: dict[str, str] | None = None) -> nx.Graph:
    """The network graph: every node (with its subnet), and every link as an edge carrying its figures, its
    source and `joins`. Nodes a link names are added even when `nodes` leaves them out."""
    g = nx.Graph()
    for n in nodes:
        g.add_node(n, subnet=(subnets or {}).get(n))
    for link in links:
        g.add_edge(link.a, link.b, rtt_ms_p50=link.rtt_ms_p50, rtt_ms_p95=link.rtt_ms_p95,
                   bandwidth_mbps=link.bandwidth_mbps, measured_at=link.measured_at, source=link.source,
                   joins=joins(link, policy, subnets))
    return g


def components(nodes: Iterable[str], links: Iterable[Link], policy: ClusterPolicy,
               subnets: dict[str, str] | None = None) -> list[frozenset[str]]:
    """Connected components of the links that join (see `joins`), over `nodes` plus any node a link names.
    Largest first, then by the smallest member, so the order is stable."""
    g = build(nodes, links, policy, subnets)
    joined = nx.subgraph_view(g, filter_edge=lambda a, b: g.edges[a, b]["joins"])
    return sorted((frozenset(c) for c in nx.connected_components(joined)), key=lambda c: (-len(c), min(c)))


def assign_ids(clusters: list[frozenset[str]], previous: dict[str, str]) -> dict[str, str]:
    """A cluster ID for every node. A cluster keeps the ID most of its members had before (larger clusters
    choose first), so IDs survive a node joining or leaving. A cluster with no earlier ID gets one derived
    from its members."""
    taken: set[str] = set()
    out: dict[str, str] = {}
    for members in clusters:
        votes = Counter(previous[n] for n in members if n in previous)
        cid = next((c for c, _ in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0])) if c not in taken), None)
        if cid is None:
            cid = _new_id(members, taken)
        taken.add(cid)
        out.update({n: cid for n in members})
    return out


def _new_id(members: frozenset[str], taken: set[str]) -> str:
    seed = ",".join(sorted(members))
    for salt in range(1000):
        cid = "c_" + hashlib.sha256(f"{seed}#{salt}".encode()).hexdigest()[:10]
        if cid not in taken:
            return cid
    raise RuntimeError("no free cluster id")  # unreachable in practice


def _worst(sides: list[dict], field: str, pick) -> float | None:
    values = [s[field] for s in sides if field in s]
    return pick(values) if values else None
