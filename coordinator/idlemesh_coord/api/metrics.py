"""Read-only metrics for dashboards (S3.6): flat JSON rows that Grafana's Infinity data source turns into tables
and time series.

Grafana reads these over HTTPS with an administrator's metrics key (scope "metrics", which works on these paths
only) instead of opening the SQLite file: the database stays private to the coordinator, and Grafana can run
anywhere. Telemetry is stored under pseudonymous node keys (D-16); these endpoints map keys back to device names
for the caller's own group only, using the salt that never leaves the coordinator.

Time range: `from_ms` and `to_ms` (Grafana's ${__from} and ${__to}), or `hours` back from now. Hour-of-week
figures use the coordinator's local time zone, which for a campus coordinator is the campus's.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Request
from starlette.concurrency import run_in_threadpool

from ..domain.access import Principal
from ..store import iso, now, parse
from ..telemetry import COORDINATOR_NODE, _ts
from .auth import current, require_metrics

MAX_RANGE = timedelta(days=90)
MAX_ROWS = 50_000
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _range(from_ms: int | None, to_ms: int | None, hours: float) -> tuple[str, str]:
    end = datetime.fromtimestamp(to_ms / 1000, timezone.utc) if to_ms else now()
    start = datetime.fromtimestamp(from_ms / 1000, timezone.utc) if from_ms else end - timedelta(hours=hours)
    start = max(start, end - MAX_RANGE)
    return _ts(start), _ts(end)


def _nodes(app, group_id: str) -> dict[str, dict]:
    """Telemetry node key -> device row, for one group's devices."""
    keys = app.state.node_keys
    return {keys.key(d["id"]): d for d in app.state.store.all(
        "SELECT id, name, capabilities, last_seen, status FROM devices WHERE group_id = ?", (group_id,))}


def _in(nodes: dict) -> tuple[str, list]:
    return ",".join("?" * len(nodes)) or "NULL", list(nodes)


def node_list(app, group_id: str) -> list[dict]:
    settings = app.state.settings
    rows = []
    for node, d in sorted(_nodes(app, group_id).items(), key=lambda kv: kv[1]["name"]):
        cap = json.loads(d["capabilities"])
        hw = cap.get("hardware") or {}
        gpus = hw.get("gpus") or []
        online = bool(d["last_seen"]) and (now() - parse(d["last_seen"])).total_seconds() <= settings.heartbeat_unassign_s
        rows.append({
            "name": d["name"], "node": node, "status": "online" if online and d["status"] == "active" else
            ("disabled" if d["status"] != "active" else "offline"),
            "node_state": cap.get("node_state") if online else None, "os": cap.get("os"), "arch": cap.get("arch"),
            "cpu": hw.get("cpu_model"), "cpu_threads": hw.get("cpu_threads"),
            "ram_gb": round(hw["ram_bytes"] / 1e9, 1) if hw.get("ram_bytes") else None,
            "unified_memory": hw.get("unified_memory"),
            "gpus": ", ".join(g["name"] for g in gpus) or None,
            "vram_gb": round(sum(g.get("vram_bytes") or 0 for g in gpus) / 1e9, 1) or None,
            "backends": ", ".join(hw.get("backends") or []) or None, "idle_source": hw.get("idle_source"),
            "power": cap.get("power"), "last_seen": d["last_seen"],
        })
    return rows


def idle_heatmap(app, group_id: str, start: str, end: str, node_name: str | None) -> list[dict]:
    """Share of idle samples by day of week and hour (local time): one row per day, a column per hour.
    Idle means what D-17 says: no input for idle_after_s, or a locked session."""
    nodes = {k: d for k, d in _nodes(app, group_id).items() if not node_name or d["name"] == node_name}
    marks, args = _in(nodes)
    idle_after = app.state.settings.idle_after_s
    total, idle = defaultdict(int), defaultdict(int)
    for r in app.state.store.all(
            f"SELECT ts, seconds_since_input, session_locked FROM idle_samples WHERE node IN ({marks})"
            f" AND ts >= ? AND ts <= ? LIMIT {MAX_ROWS * 4}", (*args, start, end)):
        t = datetime.fromisoformat(r["ts"]).astimezone()
        key = (t.weekday(), t.hour)
        total[key] += 1
        if r["session_locked"] == 1 or (r["seconds_since_input"] is not None and r["seconds_since_input"] >= idle_after):
            idle[key] += 1
    return [{"day": DAYS[d], **{f"{h:02d}": round(idle[(d, h)] / total[(d, h)], 2) if total[(d, h)] else None
                               for h in range(24)}} for d in range(7)]


def health(app, group_id: str, start: str, end: str) -> list[dict]:
    nodes = _nodes(app, group_id)
    marks, args = _in(nodes)
    rows = app.state.store.all(
        "SELECT node, ts, cpu_util, gpu_util, mem_used_gb, gpu_mem_used_gb, temp_c, cache_disk_free_gb, agent_uptime_s,"
        " worker_restarts, cap_kills, tunnel_errors, telemetry_buffer_bytes, telemetry_dropped FROM node_health"
        f" WHERE node IN ({marks}) AND ts >= ? AND ts <= ? ORDER BY ts LIMIT {MAX_ROWS}", (*args, start, end))
    return [{**{k: r[k] for k in r.keys() if k != "node"}, "node": nodes[r["node"]]["name"]} for r in rows]


def states(app, group_id: str, start: str, end: str) -> list[dict]:
    nodes = _nodes(app, group_id)
    marks, args = _in(nodes)
    rows = app.state.store.all(
        "SELECT node, ts, state, seconds_since_input, session_locked, cpu_util, gpu_util, contribution_level,"
        f" level_reason FROM idle_samples WHERE node IN ({marks}) AND ts >= ? AND ts <= ? ORDER BY ts"
        f" LIMIT {MAX_ROWS}", (*args, start, end))
    return [{"ts": r["ts"], "node": nodes[r["node"]]["name"], "state": r["state"],
             "seconds_since_input": r["seconds_since_input"],
             "session_locked": None if r["session_locked"] is None else bool(r["session_locked"]),
             "cpu_util": r["cpu_util"], "gpu_util": r["gpu_util"], "contribution_level": r["contribution_level"],
             "level_reason": r["level_reason"]} for r in rows]


def crashes(app, group_id: str, start: str, end: str) -> list[dict]:
    """Crash reports (S4.3) from the group's nodes, newest first."""
    rows = app.state.store.all(
        "SELECT c.id, c.ts, c.kind, c.version, c.build_hash, c.os, c.summary, d.name FROM crash_reports c"
        " JOIN devices d ON d.id = c.device_id WHERE c.group_id = ? AND c.ts >= ? AND c.ts <= ?"
        f" ORDER BY c.ts DESC LIMIT {MAX_ROWS}", (group_id, start, end))
    return [{"ts": r["ts"], "node": r["name"], "kind": r["kind"], "version": r["version"],
             "build_hash": r["build_hash"], "os": r["os"], "summary": r["summary"], "crash_id": r["id"]} for r in rows]


def error_rates(app, group_id: str, start: str, end: str, bucket_min: int) -> list[dict]:
    """Error events per code in fixed buckets, from the group's nodes and from the coordinator itself."""
    nodes = _nodes(app, group_id)
    marks, args = _in(nodes)
    counts: dict[tuple, int] = defaultdict(int)
    width = bucket_min * 60
    for r in app.state.store.all(
            f"SELECT node, ts, code, count FROM error_events WHERE (node IN ({marks}) OR node = ?)"
            f" AND ts >= ? AND ts <= ? LIMIT {MAX_ROWS}", (*args, COORDINATOR_NODE, start, end)):
        t = datetime.fromisoformat(r["ts"]).timestamp()
        bucket = datetime.fromtimestamp(t - t % width, timezone.utc)
        source = "coordinator" if r["node"] == COORDINATOR_NODE else nodes[r["node"]]["name"]
        counts[(bucket, r["code"], source)] += r["count"]
    return [{"ts": iso(b), "code": code, "source": source, "count": n}
            for (b, code, source), n in sorted(counts.items())]


def register(app: FastAPI) -> None:
    @app.get("/api/v1/metrics/nodes", tags=["metrics"])
    async def metrics_nodes(request: Request, p: Principal = Depends(current)):
        """Every node in the group with its hardware profile, backends and current state (S3.3, S3.4)."""
        require_metrics(p)
        return await run_in_threadpool(node_list, request.app, p.group_id)

    @app.get("/api/v1/metrics/idle-heatmap", tags=["metrics"])
    async def metrics_idle_heatmap(request: Request, p: Principal = Depends(current), from_ms: int | None = None,
                                   to_ms: int | None = None, hours: float = 24 * 28, node: str | None = None):
        """Share of time idle by day of week and hour, across the group's nodes or one node (by name)."""
        require_metrics(p)
        start, end = _range(from_ms, to_ms, hours)
        return await run_in_threadpool(idle_heatmap, request.app, p.group_id, start, end, node)

    @app.get("/api/v1/metrics/health", tags=["metrics"])
    async def metrics_health(request: Request, p: Principal = Depends(current), from_ms: int | None = None,
                             to_ms: int | None = None, hours: float = 24):
        """Node health samples (S3.5), oldest first, one row per node per sample."""
        require_metrics(p)
        start, end = _range(from_ms, to_ms, hours)
        return await run_in_threadpool(health, request.app, p.group_id, start, end)

    @app.get("/api/v1/metrics/states", tags=["metrics"])
    async def metrics_states(request: Request, p: Principal = Depends(current), from_ms: int | None = None,
                             to_ms: int | None = None, hours: float = 24):
        """Idle samples with each node's state, oldest first, for a state timeline."""
        require_metrics(p)
        start, end = _range(from_ms, to_ms, hours)
        return await run_in_threadpool(states, request.app, p.group_id, start, end)

    @app.get("/api/v1/metrics/crashes", tags=["metrics"])
    async def metrics_crashes(request: Request, p: Principal = Depends(current), from_ms: int | None = None,
                              to_ms: int | None = None, hours: float = 24 * 7):
        """Agent and worker crash reports (S4.3) from the group's nodes, newest first."""
        require_metrics(p)
        start, end = _range(from_ms, to_ms, hours)
        return await run_in_threadpool(crashes, request.app, p.group_id, start, end)

    @app.get("/api/v1/metrics/errors", tags=["metrics"])
    async def metrics_errors(request: Request, p: Principal = Depends(current), from_ms: int | None = None,
                             to_ms: int | None = None, hours: float = 24, bucket_min: int = 5):
        """Error events per code per time bucket, by source (a node's name, or coordinator)."""
        require_metrics(p)
        start, end = _range(from_ms, to_ms, hours)
        return await run_in_threadpool(error_rates, request.app, p.group_id, start, end,
                                       min(max(bucket_min, 1), 1440))
