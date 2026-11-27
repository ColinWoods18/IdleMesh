"""Each node's link to this coordinator: the bandwidth test through its tunnel and when it is due (S5.2, D-25).

The coordinator runs llama-server, so it is the head every remote stage talks to (llama.cpp RPC is hub and
spoke). A token's traffic to a stage crosses exactly this link, which makes it the figure placement needs
(S5.3), not cluster membership.

A test is due on events, not on a scan (D-25): the first time a node's worker is reachable, after the node
changes network, and otherwise every `probes.bandwidth_every_h`. The slow refresh runs only in off-hours
when `probes.bandwidth_off_hours_only` is set; the event-driven tests run as soon as the node may contribute,
because without a figure placement has to leave the link out. Every result is a `net_edges` row, method
`tunnel_test`, with the tunnel RTT too when it was timed. Agents cannot upload this method, so it always means a
coordinator-timed figure.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

from .. import obs
from ..store import Store, iso, now, parse
from ..telemetry import COORDINATOR_NODE as COORDINATOR

log = logging.getLogger("idlemesh_coord.graph")

TUNNEL = "tunnel_test"  # net_edges.method for figures the coordinator timed itself through a node's tunnel
DEGRADED_FRACTION = 0.5  # a new bandwidth figure below half the last one is a degradation event


@dataclass(frozen=True)
class BandwidthRule:
    """When a node's link is tested (site config `probes`)."""
    every_h: float = 168.0
    off_hours_only: bool = True
    off_hours_start: str = "20:00"   # coordinator local time; the window may cross midnight
    off_hours_end: str = "07:00"
    max_age_h: float = 3 * 168.0     # how long a figure counts for placement


def in_off_hours(at: datetime, start: str, end: str) -> bool:
    """True when local time `at` is inside [start, end), a window that may cross midnight. Weekends are off-hours
    all day."""
    if at.weekday() >= 5:
        return True
    t = at.time()
    s, e = (dtime(*map(int, x.split(":"))) for x in (start, end))
    return s <= t < e if s < e else (t >= s or t < e)


def last_test(store: Store, node: str) -> dict | None:
    return store.one("SELECT ts, rtt_ms_p50, bandwidth_mbps FROM net_edges WHERE src = ? AND dst = ? AND method = ?"
                     " AND bandwidth_mbps IS NOT NULL ORDER BY ts DESC LIMIT 1", (node, COORDINATOR, TUNNEL))


def due(store: Store, node: str, rule: BandwidthRule, at: datetime | None = None) -> str | None:
    """Why `node`'s link should be tested now: "first", "network_changed" or "refresh"; None when not due.
    `at` is the coordinator's local time (for the off-hours rule)."""
    at = at or datetime.now().astimezone()
    last = last_test(store, node)
    if last is None:
        return "first"
    net = store.one("SELECT since FROM node_networks WHERE node = ?", (node,))
    if net and parse(net["since"]) > parse(last["ts"]):
        return "network_changed"
    if now() - parse(last["ts"]) < timedelta(hours=rule.every_h):
        return None
    if rule.off_hours_only and not in_off_hours(at, rule.off_hours_start, rule.off_hours_end):
        return None
    return "refresh"


def record(store: Store, node: str, bandwidth: dict, rtt: dict | None, org_id: str | None,
           site_id: str | None, reason: str) -> dict:
    """Store one test as a `net_edges` row and log a degradation event when the link got much slower."""
    before = last_test(store, node)
    at = now()
    if before:  # strictly after the last row, so a fast second test is never lost to the primary key
        at = max(at, parse(before["ts"]) + timedelta(microseconds=1))
    with store.tx() as c:
        c.execute("INSERT OR IGNORE INTO net_edges (src, dst, ts, method, rtt_ms_p50, rtt_ms_p95, bandwidth_mbps,"
                  " samples, org_id, site_id, received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (node, COORDINATOR, at.isoformat(timespec="microseconds"), TUNNEL,
                   rtt["median_ms"] if rtt else None, rtt["p95_ms"] if rtt else None, bandwidth["mbps"],
                   rtt["samples"] if rtt else None, org_id, site_id, iso(now())))
    out = {"node": node, "mbps": bandwidth["mbps"], "reason": reason, "degraded": False,
           "previous_mbps": before["bandwidth_mbps"] if before else None}
    if before and bandwidth["mbps"] < DEGRADED_FRACTION * before["bandwidth_mbps"]:
        out["degraded"] = True
        obs.log(log, logging.WARNING, "link to the coordinator degraded", error_code="LINK_DEGRADED", node=node,
                metric="bandwidth_mbps", previous=before["bandwidth_mbps"], current=bandwidth["mbps"])
    obs.log(log, logging.INFO, "link bandwidth measured", node=node, reason=reason, mbps=bandwidth["mbps"],
            up_mbps=bandwidth["up_mbps"], down_mbps=bandwidth["down_mbps"],
            rtt_ms=rtt["median_ms"] if rtt else None)
    return out


def link_mbps(store: Store, max_age_h: float) -> dict[str, tuple[float, str]]:
    """Each node's newest tested bandwidth to this coordinator, newer than `max_age_h`: {node: (mbps, ts)}."""
    since = iso(now() - timedelta(hours=max_age_h))
    out: dict[str, tuple[float, str]] = {}
    for r in store.all("SELECT src, ts, bandwidth_mbps FROM net_edges WHERE dst = ? AND method = ? AND ts >= ?"
                       " AND bandwidth_mbps IS NOT NULL ORDER BY ts", (COORDINATOR, TUNNEL, since)):
        out[r["src"]] = (r["bandwidth_mbps"], r["ts"])
    return out
