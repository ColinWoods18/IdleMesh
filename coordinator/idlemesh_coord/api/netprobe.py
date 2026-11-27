"""Bandwidth tests through node tunnels (S5.2): the background sweep, the admin trigger, and the figures placement
reads (S5.3).

The coordinator checks once a minute whether one node is due (see graph.links for when) and may be tested now:
an active device with a fresh heartbeat, a running worker behind the mutual-TLS tunnel, the owner's
diagnostics consent, a governor that would let the node contribute for a reason other than user activity
(D-12, as for the decode probe in D-23), and no part in a deployment, a load or a prefetch. One test runs at a
time, on its own task, so a slow link never delays the liveness checks. A failed test is logged with
NET_PROBE_FAILED and not retried for an hour.
"""
import asyncio
import json
import logging
import time

from fastapi import Depends, FastAPI, Request
from starlette.concurrency import run_in_threadpool

from .. import obs, telemetry
from ..graph import links
from ..store import now, parse
from ..transport import TunnelError
from .auth import current, require_admin, require_group
from .errors import ApiError, not_found

log = logging.getLogger("idlemesh_coord.netprobe")

SWEEP_EVERY_S = 60
RETRY_AFTER_FAILURE_S = 3600
LOAD_TEST_BYTES = 4 << 20  # a deployment about to load waits for this test, so it is kept short


def rule(settings) -> links.BandwidthRule:
    return links.BandwidthRule(every_h=settings.bandwidth_every_h, off_hours_only=settings.bandwidth_off_hours_only,
                               off_hours_start=settings.off_hours_start, off_hours_end=settings.off_hours_end,
                               max_age_h=3 * settings.bandwidth_every_h)


def init(app) -> None:
    app.state.net_probe = {"task": None, "running": set(), "status": {}, "retry_at": {}, "next_sweep": 0.0}
    app.state.path_mbps = {}       # device_id -> (mbps, monotonic time), measured at load without consent to store
    app.state.loading_devices = set()  # devices whose deployment is loading; their links carry model weights


def busy_devices(app) -> set[str]:
    """Devices whose link is in use: a current placement, a load in progress, or a prefetch."""
    busy = set(app.state.loading_devices)
    for g in app.state.scheduler.groups.values():
        busy.update(s["device_id"] for s in (g.placement or {}).get("stages", []))
    for pending in app.state.prefetch.values():
        busy.update(pending["manifests"])
    return busy


def _tunnel_endpoint(cap: dict) -> str | None:
    w = cap.get("worker") or {}
    return w.get("endpoint") if w.get("state") == "running" and w.get("transport") == "mtls" else None


def why_not(app, d, busy: set[str]) -> str | None:
    """Why device row `d` cannot be tested now, or None."""
    settings = app.state.settings
    cap = json.loads(d["capabilities"])
    if d["status"] != "active":
        return "device is not active"
    if not d["last_seen"] or (now() - parse(d["last_seen"])).total_seconds() > settings.heartbeat_unassign_s:
        return "no recent heartbeat"
    if not _tunnel_endpoint(cap):
        return "no running worker behind the mutual-TLS tunnel"
    if not telemetry.has_consent(json.loads(d["consent"]) if d["consent"] else None, {"net_edge"}):
        return "the owner has not consented to diagnostics"
    if d["id"] in busy or d["id"] in app.state.net_probe["running"]:
        return "the link is in use by a deployment, a load or another test"
    level = cap.get("contribution")
    if level is not None:
        if level["level"] <= 0 or level["reason"] == "user_activity":
            return "the governor does not allow contribution now"
    elif cap.get("node_state") != "idle":
        return "the node is not idle"
    return None


async def run_test(app, d, reason: str, nbytes: int) -> dict:
    """Test one device's link now and store the result. Raises TunnelError on failure."""
    store, settings, state = app.state.store, app.state.settings, app.state.net_probe
    endpoint = _tunnel_endpoint(json.loads(d["capabilities"]))
    node = app.state.node_keys.key(d["id"])
    state["running"].add(d["id"])
    try:
        result = await run_in_threadpool(app.state.tunnels.measure_bandwidth, endpoint, d["public_key"], nbytes)
    except TunnelError as e:
        state["status"][d["id"]] = {"at": now().isoformat(), "ok": False, "code": "NET_PROBE_FAILED",
                                    "message": str(e)[:300], "reason": reason}
        state["retry_at"][d["id"]] = time.monotonic() + RETRY_AFTER_FAILURE_S
        obs.log(log, logging.WARNING, "bandwidth test failed", error_code="NET_PROBE_FAILED", node=node,
                reason=reason, detail=str(e)[:300])
        raise
    finally:
        state["running"].discard(d["id"])
    app.state.path_mbps[d["id"]] = (result["mbps"], time.monotonic())
    out = await run_in_threadpool(links.record, store, node, result, None, settings.org_id, settings.site_id, reason)
    state["status"][d["id"]] = {"at": now().isoformat(), "ok": True, "reason": reason, **result,
                                "degraded": out["degraded"]}
    state["retry_at"].pop(d["id"], None)
    return {**result, "reason": reason, "degraded": out["degraded"], "previous_mbps": out["previous_mbps"]}


def _next_due(app) -> tuple[object, str] | None:
    store, state = app.state.store, app.state.net_probe
    r, busy, keys = rule(app.state.settings), busy_devices(app), app.state.node_keys
    for d in store.all("SELECT * FROM devices WHERE status = 'active' ORDER BY id"):
        if state["retry_at"].get(d["id"], 0) > time.monotonic() or why_not(app, d, busy):
            continue
        reason = links.due(store, keys.key(d["id"]), r)
        if reason:
            return d, reason
    return None


async def _sweep_once(app) -> None:
    pick = await run_in_threadpool(_next_due, app)
    if pick:
        d, reason = pick
        try:
            await run_test(app, d, reason, app.state.settings.bandwidth_test_mb << 20)
        except TunnelError:
            pass  # logged and counted in run_test


def maybe_sweep(app) -> None:
    """Called from the liveness monitor every second: start one sweep a minute, never two at once."""
    state = app.state.net_probe
    if time.monotonic() < state["next_sweep"] or (state["task"] and not state["task"].done()):
        return
    state["next_sweep"] = time.monotonic() + SWEEP_EVERY_S

    async def guarded():
        try:
            await _sweep_once(app)
        except Exception:  # noqa: BLE001 - a sweep must never stop the coordinator
            log.exception("bandwidth sweep failed", extra={"error_code": "INTERNAL"})
    state["task"] = asyncio.get_running_loop().create_task(guarded())


async def wait_idle(app, device_ids: set[str], timeout: float = 90) -> None:
    """Wait for a running test on any of these devices to finish before their links carry a deployment."""
    deadline = time.monotonic() + timeout
    while app.state.net_probe["running"] & device_ids and time.monotonic() < deadline:
        await asyncio.sleep(0.1)


def figures(app) -> dict[str, tuple[float, str]]:
    """{device_id: (mbps, source)} for placement: a stored test newer than 3 x bandwidth_every_h, else a test
    timed at load in the last day (kept in memory when the owner has not consented to storing it)."""
    keys, store = app.state.node_keys, app.state.store
    stored = links.link_mbps(store, rule(app.state.settings).max_age_h)
    out: dict[str, tuple[float, str]] = {}
    for d in store.all("SELECT id FROM devices WHERE status != 'revoked'"):
        hit = stored.get(keys.key(d["id"]))
        if hit:
            out[d["id"]] = (hit[0], "tunnel_test")
    for device_id, (mbps, at) in app.state.path_mbps.items():
        if device_id not in out and time.monotonic() - at <= 86_400:
            out[device_id] = (mbps, "tunnel_test")
    return out


async def test_at_load(app, d) -> dict | None:
    """A deployment is about to use a link with no figure: time it now, briefly, and keep the figure (S5.3).
    Stored only with the owner's diagnostics consent; otherwise kept in memory for planning."""
    endpoint = _tunnel_endpoint(json.loads(d["capabilities"]))
    if not endpoint:
        return None
    if telemetry.has_consent(json.loads(d["consent"]) if d["consent"] else None, {"net_edge"}):
        try:
            return await run_test(app, d, "deployment", LOAD_TEST_BYTES)
        except TunnelError:
            return None
    try:
        result = await run_in_threadpool(app.state.tunnels.measure_bandwidth, endpoint, d["public_key"],
                                         LOAD_TEST_BYTES)
    except TunnelError as e:
        obs.log(log, logging.WARNING, "bandwidth test failed", error_code="NET_PROBE_FAILED",
                reason="deployment", detail=str(e)[:300])
        return None
    app.state.path_mbps[d["id"]] = (result["mbps"], time.monotonic())
    return {**result, "reason": "deployment"}


def register(app: FastAPI) -> None:
    @app.post("/api/v1/groups/{group_id}/devices/{device_id}/bandwidth-test", tags=["devices"])
    async def bandwidth_test(group_id: str, device_id: str, request: Request, p=Depends(current)):
        """Test a device's link to this coordinator now (S5.2, D-25 admin request). Group administrators only.
        Skips the off-hours rule, but not the others: the owner's consent, the governor, and a link in use."""
        require_group(p, group_id)
        require_admin(p)
        a = request.app
        d = a.state.store.one("SELECT * FROM devices WHERE id = ? AND group_id = ?", (device_id, group_id))
        if not d:
            raise not_found()
        reason = why_not(a, d, busy_devices(a))
        if reason:
            raise ApiError(409, "NET_PROBE_REFUSED", f"Cannot test this device's link now: {reason}.", True)
        try:
            return {"device_id": device_id, **await run_test(a, d, "admin", a.state.settings.bandwidth_test_mb << 20)}
        except TunnelError as e:
            raise ApiError(503, "NET_PROBE_FAILED", f"The bandwidth test failed: {e}", True) from None

    @app.get("/api/v1/groups/{group_id}/network/probes", tags=["devices"])
    async def probe_status(group_id: str, request: Request, p=Depends(current)):
        """The last bandwidth test of each device since the coordinator started, and when the next is due."""
        require_group(p, group_id)
        require_admin(p)
        return {"devices": await run_in_threadpool(status_rows, request.app, group_id)}


def status_rows(app, group_id: str) -> list[dict]:
    store, keys, state = app.state.store, app.state.node_keys, app.state.net_probe
    busy, r = busy_devices(app), rule(app.state.settings)
    stored = links.link_mbps(store, r.max_age_h)
    out = []
    for d in store.all("SELECT * FROM devices WHERE group_id = ? AND status != 'revoked' ORDER BY name", (group_id,)):
        node = keys.key(d["id"])
        hit = stored.get(node)
        out.append({"device_id": d["id"], "name": d["name"], "link_mbps": hit[0] if hit else None,
                    "measured_at": hit[1] if hit else None, "due": links.due(store, node, r),
                    "blocked_by": why_not(app, d, busy), "last_test": state["status"].get(d["id"])})
    return out


__all__ = ["busy_devices", "figures", "init", "maybe_sweep", "register", "run_test", "test_at_load", "wait_idle"]
