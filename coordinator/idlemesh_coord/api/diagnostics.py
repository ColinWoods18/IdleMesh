"""Remote diagnosis (D-11, Sprint 4): crash reports (S4.3), diagnostic bundles (S4.4) and a temporary remote log
level (S4.5). Together they let an administrator find out why a node failed without visiting it.

Everything here needs the device owner's **diagnostics** consent (S2.6): without it the coordinator refuses
uploads and requests (DIAGNOSTICS_NO_CONSENT), and the agent sends nothing. The device's owner and group
administrators can request and read diagnostics; other members cannot see them.

Flow for a bundle: an administrator asks (POST .../diagnostics) -> the next heartbeat reply lists the request ->
the agent builds the bundle (recent logs, versions, probes, health, redacted config) and uploads it ->
the administrator reads it (GET .../diagnostics/{bundle_id}). Bundles are kept for BUNDLE_RETENTION_DAYS.
"""
import json
import logging
from datetime import timedelta

from fastapi import Depends, FastAPI, Request
from starlette.concurrency import run_in_threadpool

from .. import obs
from ..domain.access import Principal
from ..security import new_id
from ..store import Store, iso, now, parse
from .auth import current, device_by_token, require_group
from .errors import ApiError, not_found
from .schemas import CrashReportIn, LogLevelIn

log = logging.getLogger("idlemesh_coord.diagnostics")

BUNDLE_RETENTION_DAYS = 14
MAX_BUNDLE_BYTES = 1 << 20  # the coordinator's request body limit


def has_diagnostics_consent(d) -> bool:
    return bool(d["consent"] and json.loads(d["consent"]).get("diagnostics"))


def _require_consent(d) -> None:
    if not has_diagnostics_consent(d):
        raise ApiError(409, "DIAGNOSTICS_NO_CONSENT", "The device owner has not consented to diagnostics.")


def _owned_device(request: Request, p: Principal, group_id: str, device_id: str):
    """A device the caller may diagnose: their own, or any in the group for an administrator."""
    require_group(p, group_id)
    d = request.app.state.store.one("SELECT * FROM devices WHERE id = ? AND group_id = ?", (device_id, group_id))
    if not d or (d["owner_id"] != p.member_id and not p.is_admin):
        raise not_found()
    return d


def heartbeat_extras(store: Store, d) -> dict:
    """What a heartbeat reply carries for diagnostics: pending bundle requests and an active log-level window."""
    out: dict = {}
    if not has_diagnostics_consent(d):
        return out
    pending = store.all("SELECT id FROM diagnostic_bundles WHERE device_id = ? AND state = 'requested'"
                        " ORDER BY requested_at", (d["id"],))
    if pending:
        out["diagnostics_requests"] = [r["id"] for r in pending]
    override = json.loads(d["log_override"]) if d["log_override"] else None
    if override:
        remaining = (parse(override["until"]) - now()).total_seconds()
        if remaining > 0:
            out["log_level"] = {"level": override["level"], "remaining_s": int(remaining)}
    return out


def prune(store: Store, retention_days: int) -> int:
    """Delete old bundles (BUNDLE_RETENTION_DAYS) and crash reports (the site's telemetry retention)."""
    removed = 0
    with store.tx() as c:
        removed += c.execute("DELETE FROM diagnostic_bundles WHERE requested_at < ?",
                             (iso(now() - timedelta(days=BUNDLE_RETENTION_DAYS)),)).rowcount
        removed += c.execute("DELETE FROM crash_reports WHERE received_at < ?",
                             (iso(now() - timedelta(days=retention_days)),)).rowcount
    return removed


def _summary(body: CrashReportIn) -> str:
    first = body.message.strip().splitlines()[0] if body.message.strip() else ""
    if body.kind == "worker":
        return f"worker exited ({body.exit_status or 'unknown status'}): {first}"[:300]
    return f"agent panicked at {body.location or 'unknown location'}: {first}"[:300]


def register(app: FastAPI) -> None:
    # --- Crash reports (S4.3) ------------------------------------------------------------------
    @app.post("/api/v1/devices/{device_id}/crash-reports", status_code=201, tags=["diagnostics"])
    async def upload_crash_report(device_id: str, body: CrashReportIn, request: Request):
        """An agent uploads a crash report it saved (its own panic, or its worker's exit). Repeats are ignored."""
        d = device_by_token(request, device_id)
        _require_consent(d)
        store: Store = request.app.state.store
        with store.tx() as c:
            new = c.execute(
                "INSERT OR IGNORE INTO crash_reports (id, device_id, group_id, kind, ts, received_at, version,"
                " build_hash, os, summary, body) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (body.crash_id, device_id, d["group_id"], body.kind, iso(body.ts), iso(now()), body.version,
                 body.build_hash, body.os, _summary(body), body.model_dump_json())).rowcount
        if new:
            code = "AGENT_PANIC" if body.kind == "agent" else "WORKER_CRASHED"
            obs.log(log, logging.WARNING, f"crash report received: {_summary(body)}", error_code=code,
                    crash_id=body.crash_id, kind=body.kind, agent_version=body.version, build_hash=body.build_hash)
        return {"crash_id": body.crash_id, "duplicate": not new}

    @app.get("/api/v1/groups/{group_id}/devices/{device_id}/crash-reports", tags=["diagnostics"])
    async def list_crash_reports(group_id: str, device_id: str, request: Request, p: Principal = Depends(current)):
        _owned_device(request, p, group_id, device_id)
        rows = request.app.state.store.all(
            "SELECT id, kind, ts, received_at, version, build_hash, os, summary FROM crash_reports"
            " WHERE device_id = ? ORDER BY ts DESC LIMIT 200", (device_id,))
        return {"crash_reports": [{"crash_id": r["id"], **{k: r[k] for k in r.keys() if k != "id"}} for r in rows]}

    @app.get("/api/v1/groups/{group_id}/devices/{device_id}/crash-reports/{crash_id}", tags=["diagnostics"])
    async def get_crash_report(group_id: str, device_id: str, crash_id: str, request: Request,
                               p: Principal = Depends(current)):
        _owned_device(request, p, group_id, device_id)
        r = request.app.state.store.one("SELECT * FROM crash_reports WHERE id = ? AND device_id = ?",
                                        (crash_id, device_id))
        if not r:
            raise not_found()
        return {"received_at": r["received_at"], "summary": r["summary"], **json.loads(r["body"])}

    # --- Diagnostic bundles (S4.4) -------------------------------------------------------------
    @app.post("/api/v1/groups/{group_id}/devices/{device_id}/diagnostics", status_code=202, tags=["diagnostics"])
    async def request_bundle(group_id: str, device_id: str, request: Request, p: Principal = Depends(current)):
        """Ask a node for a diagnostic bundle. It is built and uploaded after the node's next heartbeat."""
        d = _owned_device(request, p, group_id, device_id)
        _require_consent(d)
        bid = new_id("diag")
        store: Store = request.app.state.store
        with store.tx() as c:
            c.execute("INSERT INTO diagnostic_bundles (id, device_id, group_id, requested_by, requested_at, state)"
                      " VALUES (?,?,?,?,?,?)", (bid, device_id, group_id, p.member_id, iso(now()), "requested"))
            store.audit(c, group_id, p.member_id, "diagnostics.request", device_id)
        obs.log(log, logging.INFO, "diagnostic bundle requested", bundle_id=bid, node=device_id)
        return {"bundle_id": bid, "state": "requested"}

    @app.get("/api/v1/groups/{group_id}/devices/{device_id}/diagnostics", tags=["diagnostics"])
    async def list_bundles(group_id: str, device_id: str, request: Request, p: Principal = Depends(current)):
        _owned_device(request, p, group_id, device_id)
        rows = request.app.state.store.all(
            "SELECT id, requested_at, state, uploaded_at, size_bytes, error FROM diagnostic_bundles"
            " WHERE device_id = ? ORDER BY requested_at DESC", (device_id,))
        return {"bundles": [{"bundle_id": r["id"], **{k: r[k] for k in r.keys() if k != "id"}} for r in rows]}

    @app.get("/api/v1/groups/{group_id}/devices/{device_id}/diagnostics/{bundle_id}", tags=["diagnostics"])
    async def get_bundle(group_id: str, device_id: str, bundle_id: str, request: Request,
                         p: Principal = Depends(current)):
        _owned_device(request, p, group_id, device_id)
        r = request.app.state.store.one("SELECT * FROM diagnostic_bundles WHERE id = ? AND device_id = ?",
                                        (bundle_id, device_id))
        if not r:
            raise not_found()
        with request.app.state.store.tx() as c:
            request.app.state.store.audit(c, group_id, p.member_id, "diagnostics.read", bundle_id)
        return {"bundle_id": r["id"], "state": r["state"], "requested_at": r["requested_at"],
                "uploaded_at": r["uploaded_at"], "size_bytes": r["size_bytes"], "error": r["error"],
                "bundle": json.loads(r["body"]) if r["body"] else None}

    @app.post("/api/v1/devices/{device_id}/diagnostics/{bundle_id}", tags=["diagnostics"])
    async def upload_bundle(device_id: str, bundle_id: str, request: Request):
        """The agent's answer to a request: the bundle as one JSON object (at most 1 MiB)."""
        d = device_by_token(request, device_id)
        _require_consent(d)
        raw = await request.body()
        try:
            bundle = json.loads(raw)
        except ValueError:
            raise ApiError(400, "INVALID_FIELDS", "The bundle must be one JSON object.") from None
        if not isinstance(bundle, dict) or "versions" not in bundle:
            raise ApiError(400, "INVALID_FIELDS", "The bundle must be a JSON object with a versions section.")
        store: Store = request.app.state.store

        def save() -> int:
            with store.tx() as c:
                return c.execute(
                    "UPDATE diagnostic_bundles SET state = ?, uploaded_at = ?, size_bytes = ?, body = ?, error = ?"
                    " WHERE id = ? AND device_id = ? AND state = 'requested'",
                    ("failed" if bundle.get("error") else "uploaded", iso(now()), len(raw),
                     json.dumps(bundle), str(bundle.get("error"))[:300] if bundle.get("error") else None,
                     bundle_id, device_id)).rowcount
        if not await run_in_threadpool(save):
            raise not_found()
        obs.log(log, logging.INFO, "diagnostic bundle received", bundle_id=bundle_id, size_bytes=len(raw))
        return {"bundle_id": bundle_id, "state": "uploaded"}

    # --- Remote log level (S4.5) ---------------------------------------------------------------
    @app.post("/api/v1/groups/{group_id}/devices/{device_id}/log-level", tags=["diagnostics"])
    async def set_log_level(group_id: str, device_id: str, body: LogLevelIn, request: Request,
                            p: Principal = Depends(current)):
        """Open a window in which one node logs at `level`; it reverts by itself after `minutes`, on the node too,
        even if the coordinator is unreachable then. minutes=0 closes the window now."""
        d = _owned_device(request, p, group_id, device_id)
        _require_consent(d)
        until = now() + timedelta(minutes=body.minutes)
        override = json.dumps({"level": body.level, "until": iso(until)}) if body.minutes else None
        store: Store = request.app.state.store
        with store.tx() as c:
            c.execute("UPDATE devices SET log_override = ? WHERE id = ?", (override, device_id))
            store.audit(c, group_id, p.member_id, "device.log_level", device_id,
                        f"{body.level} for {body.minutes} min" if body.minutes else "reset")
        obs.log(log, logging.INFO, "node log level window " + ("opened" if body.minutes else "closed"),
                node=device_id, log_level=body.level, minutes=body.minutes)
        return {"device_id": device_id, "level": body.level if body.minutes else None,
                "until": iso(until) if body.minutes else None}
