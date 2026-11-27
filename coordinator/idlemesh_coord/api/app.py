"""Group gateway and coordinator HTTP application (spec 6.1, 9.2)."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .. import graph, obs, partition, telemetry
from ..catalog import CATALOG, MODEL_FILES, PROBE_MODEL
from ..config import Settings
from ..domain.access import Branch, ForkError, Principal, Role, Scope, Visibility, can_create_with_visibility, \
    can_delete_branch, can_read_branch, can_write_branch, plan_fork
from ..domain.fairqueue import QueueFull
from ..domain.lifecycle import TERMINAL, RequestState
from ..domain.placement import DeviceProfile, PlacementPolicy, plan, reestimate_with_path_rtt
from ..runtime.base import RuntimeFailure
from ..runtime.fake import FakeRuntime
from ..scheduler import Scheduler
from ..security import Passwords, fingerprint, matches, new_id, new_secret, verifier
from ..store import Store, iso, now, parse
from ..tokens import context_tokens
from ..transport import CoordinatorIdentity, TunnelError, TunnelManager, cert_fingerprint, is_certificate
from . import compat, diagnostics, errors, metrics
from .auth import SESSION_COOKIE, bearer, current, device_by_token, require_admin, require_group, require_session
from .errors import ApiError, not_found
from .schemas import (CodeIn, ConversationIn, CreateGroupIn, CredentialIn, DeploymentIn, EnrollIn, ForkIn,
                      HeartbeatIn, InviteIn, LoginIn, PolicyPatch, RecoverIn, RedeemIn, RequestIn, TelemetryBatchIn)

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}

log = logging.getLogger("idlemesh_coord.api")
access_log = logging.getLogger("idlemesh_coord.access")


def create_app(settings: Settings | None = None, runtime=None) -> FastAPI:
    settings = settings or Settings()
    if runtime is None:
        if os.environ.get("IDLEMESH_RUNTIME") == "llamacpp":
            from ..runtime.llamacpp import LlamaCppRuntime
            runtime = LlamaCppRuntime.from_env()
        else:
            runtime = FakeRuntime(settings.fake_token_delay_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scheduler = Scheduler(app.state.store, runtime, settings)
        app.state.scheduler.start()
        _restore_deployments(app)
        monitor = asyncio.get_running_loop().create_task(_liveness_monitor(app))
        yield
        monitor.cancel()
        await app.state.scheduler.stop()
        for gid, g in list(app.state.scheduler.groups.items()):
            if g.deployment_id:
                await unload(app, g.deployment_id)
        app.state.tunnels.close_all()

    app = FastAPI(title="DIFA Coordinator", version="0.1.0", lifespan=lifespan,
                  description="Distributed Inference For All coordinator prototype. Fake runtime only.")
    app.state.settings, app.state.runtime = settings, runtime
    app.state.site = None  # the parsed site config, when the coordinator was started with one (S1.12)
    app.state.store = Store(settings.db_path)
    tunnel_dir = settings.tunnel_dir or os.path.dirname(os.path.abspath(settings.db_path))
    app.state.tunnels = TunnelManager(CoordinatorIdentity(tunnel_dir))
    app.state.prefetch = {}  # group_id -> pending partition download before a deployment loads (FR 30)
    app.state.prefetch_errors = {}
    app.state.control_fingerprint = (cert_fingerprint(open(settings.control_cert).read())
                                     if settings.control_cert else None)
    app.state.path_rtt = {}  # device_id -> (mean ms through the worker tunnel, monotonic time measured)
    app.state.passwords = Passwords(settings.argon2_fast)
    app.state.node_keys = telemetry.NodeKeys(os.path.dirname(os.path.abspath(settings.db_path)),
                                             settings.pseudonymize_nodes)
    app.state.login_attempts = {}
    obs.install_error_counter()
    errors.install(app)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        """Trace ID, body limit, access log and the last-resort error handler for every exchange (D-11)."""
        tid = obs.accept_trace_id(request.headers.get("x-trace-id"))
        request.state.trace_id = tid
        with obs.bound(trace_id=tid):
            started = time.perf_counter()
            length = request.headers.get("content-length")
            if length and int(length) > settings.max_body_bytes:
                request.state.error_code = "BODY_TOO_LARGE"
                response = JSONResponse(errors.envelope("BODY_TOO_LARGE", "Request body exceeds 1 MiB."), 413)
            else:
                try:
                    response = await call_next(request)
                except Exception:  # noqa: BLE001 - anything unhandled becomes a catalogued, traceable 500
                    request.state.error_code = "INTERNAL"
                    log.exception("unhandled error", extra={"error_code": "INTERNAL"})
                    response = JSONResponse(errors.envelope(
                        "INTERNAL", f"Something went wrong. Quote trace ID {tid} when reporting it.", True,
                        trace_id=tid), 500)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if request.url.path.endswith("/heartbeat"):
                # Agents subtract this from their measured round trip, so the RTT they report is the network's,
                # not the coordinator's database write (spec 7.2 fallback input when no tunnel RTT is known).
                response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.2f}"
            response.headers["X-Trace-Id"] = tid
            _access_log(request, response.status_code, elapsed_ms)
            return response

    register(app)
    compat.register(app)
    metrics.register(app)
    diagnostics.register(app)
    return app


def S(request: Request) -> Store:
    return request.app.state.store


# Routine node traffic (every node calls these every few seconds) is logged at debug level.
_ROUTINE = ("/heartbeat", "/model-file")


def _access_log(request: Request, status: int, elapsed_ms: float) -> None:
    """One line per HTTP exchange. The path is logged without its query string, which can carry secrets."""
    code = getattr(request.state, "error_code", None)
    if status >= 500:
        level = logging.ERROR
    elif status >= 400:
        level = logging.INFO
    else:
        level = logging.DEBUG if request.url.path.endswith(_ROUTINE) else logging.INFO
    # The node is identified inside the endpoint, after this middleware's context was copied, so bind it here.
    with obs.bound(node_id=getattr(request.state, "node_id", None)):
        obs.log(access_log, level, f"{request.method} {request.url.path} {status}", error_code=code,
                method=request.method, path=request.url.path, status=status, duration_ms=round(elapsed_ms, 2))


def _restore_deployments(app) -> None:
    """On restart, mark interrupted attempts and require deployments to be reloaded (spec 10)."""
    store = app.state.store
    with store.tx() as c:
        c.execute("UPDATE requests SET status = 'failed', error_code = 'COORDINATOR_RESTART', updated_at = ? "
                  "WHERE status NOT IN ('completed','cancelled','failed','expired')", (iso(now()),))
        c.execute("UPDATE deployments SET state = 'unavailable' WHERE state = 'ready'")


_UNSET = object()


def probe_offer(app) -> dict | None:
    """The decode-probe model this coordinator can serve (S5.6), or None. Worked out once: the runtime
    verifies the file's SHA-256 the first time, which takes a moment for a large file."""
    offer = getattr(app.state, "probe_offer", _UNSET)
    if offer is _UNSET:
        offer = None
        meta, model_path = MODEL_FILES.get(PROBE_MODEL), getattr(app.state.runtime, "model_path", None)
        if meta and model_path:
            try:
                path = model_path(PROBE_MODEL)
                offer = {"catalog": PROBE_MODEL, "file": meta["file"], "sha256": meta["sha256"],
                         "nbytes": path.stat().st_size, "license": meta["license"],
                         "url": "/api/v1/devices/{device_id}/probe-model"}
            except RuntimeFailure as e:
                obs.log(log, logging.WARNING, "decode-probe model unavailable; workers keep the memory-copy figure",
                        error_code="PROBE_FAILED", reason=e.code)
        app.state.probe_offer = offer
    return offer


def cluster_policy(settings: Settings) -> graph.ClusterPolicy:
    return graph.ClusterPolicy(max_rtt_ms=settings.cluster_max_rtt_ms, min_mbps=settings.cluster_min_mbps)


def recompute_clusters(app) -> dict:
    """Recompute the network clusters over every enrolled, unrevoked device (S5.4)."""
    store, settings, keys = app.state.store, app.state.settings, app.state.node_keys
    nodes = [keys.key(r["id"]) for r in store.all("SELECT id FROM devices WHERE status != 'revoked'")]
    return graph.recompute(store, nodes, cluster_policy(settings), settings.cluster_max_age_h)


async def _liveness_monitor(app) -> None:
    """Stop using a deployment as soon as a required device misses heartbeats (FR 04, spec 10)."""
    store, sched, settings = app.state.store, app.state.scheduler, app.state.settings
    next_prune = 0.0
    next_error_flush = time.monotonic() + 60
    next_clusters = time.monotonic() + 5
    while True:
        await asyncio.sleep(1)
        if time.monotonic() >= next_error_flush:  # the coordinator's own error counts, once a minute (S3.6)
            next_error_flush = time.monotonic() + 60
            try:
                await run_in_threadpool(telemetry.record_coordinator_errors, store, obs.take_error_counts(),
                                        settings.org_id, settings.site_id)
            except Exception:  # noqa: BLE001 - never let metrics stop the liveness monitor
                log.exception("cannot record error counts", extra={"error_code": "INTERNAL"})
        if time.monotonic() >= next_clusters:  # network clusters (S5.4)
            next_clusters = time.monotonic() + settings.cluster_every_s
            try:
                await run_in_threadpool(recompute_clusters, app)
            except Exception:  # noqa: BLE001 - clustering must never stop the liveness monitor
                log.exception("cannot recompute clusters", extra={"error_code": "INTERNAL"})
        if time.monotonic() >= next_prune:  # telemetry retention (S2.3), hourly
            next_prune = time.monotonic() + 3600
            try:
                await run_in_threadpool(telemetry.prune, store, settings.telemetry_retention_days)
                await run_in_threadpool(diagnostics.prune, store, settings.telemetry_retention_days)
            except Exception:  # noqa: BLE001 - retention must never stop the liveness monitor
                log.exception("telemetry retention failed", extra={"error_code": "INTERNAL"})
        for gid in list(app.state.prefetch):
            try:
                await _advance_prefetch(app, gid)
            except Exception as e:  # noqa: BLE001 - never let one group stop the monitor
                pending = app.state.prefetch.pop(gid, None)
                if pending:
                    _fail_deployment(store, gid, pending["deployment_id"], "PREFETCH_ERROR", str(e)[:200])
        for gid, g in list(sched.groups.items()):
            if not g.placement:
                continue
            for stage in g.placement["stages"]:
                d = store.one("SELECT last_seen, status, capabilities, policy FROM devices WHERE id = ?",
                              (stage["device_id"],))
                stale = (not d or d["status"] != "active" or not d["last_seen"]
                         or (now() - parse(d["last_seen"])).total_seconds() > settings.heartbeat_unassign_s)
                # Owner pause, a lowered cap, or a stopped worker is a capacity change: drain (FR 09).
                drained = False
                if d and not stale:
                    worker = json.loads(d["capabilities"]).get("worker")
                    pol = json.loads(d["policy"])
                    drained = (pol.get("paused") or pol.get("reported_paused")
                               or bool(worker and worker["state"] != "running"))
                if stale or drained:
                    why = "unavailable" if stale else "drained by owner policy or worker stop"
                    await mark_unavailable(app, gid, g.deployment_id, f"device {stage['device_id']} {why}")
                    break


async def mark_unavailable(app, group_id: str, deployment_id: str, reason: str) -> None:
    store, sched = app.state.store, app.state.scheduler
    with store.tx() as c:
        c.execute("UPDATE deployments SET state = 'unavailable' WHERE id = ?", (deployment_id,))
        store.audit(c, group_id, None, "deployment.unavailable", deployment_id, reason)
    sched.device_lost(group_id)
    sched.set_deployment(group_id, None, None)
    await unload(app, deployment_id, worker_lost=reason)


def _fail_deployment(store: Store, group_id: str, dep_id: str, code: str, detail: str | None = None) -> None:
    with store.tx() as c:
        c.execute("UPDATE deployments SET state = 'failed' WHERE id = ?", (dep_id,))
        store.audit(c, group_id, None, "deployment.failed", dep_id, f"{code}: {detail}" if detail else code)


async def load_now(app, group_id: str, dep_id: str, explanation: dict, member_id: str | None) -> dict:
    """Open tunnels, load the backend, and make the deployment current. Marks it failed on error."""
    store = app.state.store
    try:
        await open_tunnels(app, store, dep_id, explanation)
        loaded = await app.state.runtime.load_deployment(dep_id, explanation)
    except RuntimeFailure as e:
        await run_in_threadpool(app.state.tunnels.close, dep_id)
        _fail_deployment(store, group_id, dep_id, e.code)
        raise
    with store.tx() as c:
        # Store the plan as loaded (transport and loopback forwarder per stage), not as first planned.
        c.execute("UPDATE deployments SET state = 'ready', placement = ? WHERE id = ?", (json.dumps(explanation), dep_id))
        store.audit(c, group_id, member_id, "deployment.ready", dep_id)
    app.state.scheduler.set_deployment(group_id, dep_id, explanation)
    return loaded


def _prefetch_progress(store: Store, pending: dict) -> list[dict]:
    out = []
    for device_id, man in pending["manifests"].items():
        d = store.one("SELECT name, capabilities FROM devices WHERE id = ?", (device_id,))
        rep_ = json.loads(d["capabilities"]).get("prefetch") if d else None
        if rep_ and rep_["deployment_id"] != pending["deployment_id"]:
            rep_ = None
        state = "ready" if not man["tensors"] else (rep_["state"] if rep_ else "waiting")
        out.append({"device_id": device_id, "name": d["name"] if d else None, "state": state,
                    "bytes_done": rep_["bytes_done"] if rep_ else 0, "bytes_total": man["bytes_prefetch"],
                    "bytes_stage": man["bytes_stage"], "error": rep_.get("error") if rep_ else None})
    return out


async def _advance_prefetch(app, group_id: str) -> None:
    """Load once every device reports its partition verified; fail on a device error or timeout."""
    store, pending = app.state.store, app.state.prefetch[group_id]
    progress = _prefetch_progress(store, pending)
    failed = [p for p in progress if p["state"] == "failed"]
    timed_out = time.monotonic() - pending["started"] > app.state.settings.prefetch_timeout_s
    if failed or timed_out:
        del app.state.prefetch[group_id]
        detail = (f"{failed[0]['name']}: {failed[0]['error']}" if failed else "prefetch timed out")
        code = "LICENSE_NOT_ACCEPTED" if failed and "license" in (failed[0]["error"] or "") else "PREFETCH_FAILED"
        _fail_deployment(store, group_id, pending["deployment_id"], code, detail)
        pending["error"] = {"code": code, "message": detail}
        app.state.prefetch_errors[group_id] = pending["error"]
        return
    if all(p["state"] == "ready" for p in progress):
        del app.state.prefetch[group_id]
        try:
            await load_now(app, group_id, pending["deployment_id"], pending["explanation"], pending["member_id"])
        except RuntimeFailure as e:
            app.state.prefetch_errors[group_id] = {"code": e.code, "message": e.message}


async def unload(app, deployment_id: str, worker_lost: str | None = None) -> None:
    """Stop the backend first, then its tunnels, so no stage is left half connected."""
    await app.state.runtime.unload_deployment(deployment_id, worker_lost=worker_lost)
    await run_in_threadpool(app.state.tunnels.close, deployment_id)


async def open_tunnels(app, store: Store, deployment_id: str, explanation: dict) -> None:
    """Route every remote stage whose agent runs the mTLS tunnel through a loopback forwarder."""
    local = getattr(app.state.runtime, "local_device", None)
    measured: dict[str, dict] = {}
    for st in explanation["stages"]:
        if st["name"] == local or not st.get("endpoint"):
            continue
        d = store.one("SELECT public_key, capabilities FROM devices WHERE id = ?", (st["device_id"],))
        worker = json.loads(d["capabilities"]).get("worker") or {}
        st["transport"] = worker.get("transport", "plaintext")
        if st["transport"] != "mtls":
            continue
        try:
            await run_in_threadpool(app.state.tunnels.probe, st["endpoint"], d["public_key"])
        except TunnelError as e:
            await run_in_threadpool(app.state.tunnels.close, deployment_id)
            raise RuntimeFailure("TUNNEL_FAILED", f"{st['name']}: {e}") from None
        try:  # time the token path while the worker is still idle; heartbeat RTT stays as the fallback
            measured[st["device_id"]] = await run_in_threadpool(app.state.tunnels.measure_rtt, st["endpoint"],
                                                                d["public_key"])
            app.state.path_rtt[st["device_id"]] = (measured[st["device_id"]]["mean_ms"], time.monotonic())
        except TunnelError as e:
            st["rtt_measurement_error"] = str(e)
        try:
            st["rpc_endpoint"] = await run_in_threadpool(app.state.tunnels.open, deployment_id, st["endpoint"],
                                                         d["public_key"])
        except TunnelError as e:
            await run_in_threadpool(app.state.tunnels.close, deployment_id)
            raise RuntimeFailure("TUNNEL_FAILED", f"{st['name']}: {e}") from None
    reestimate_with_path_rtt(explanation, measured)


def _branch(row) -> Branch:
    return Branch(row["id"], row["group_id"], row["owner_id"], Visibility(row["visibility"]))


def _load_branch(store: Store, p: Principal, branch_id: str):
    row = store.one("SELECT * FROM branches WHERE id = ? AND deleted_at IS NULL", (branch_id,))
    if not row or not can_read_branch(p, _branch(row)):
        raise not_found()
    return row


PATH_RTT_MAX_AGE_S = 900


def _planning_rtt(path_rtt: dict | None, device_id: str, heartbeat_rtt: float) -> tuple[float, str]:
    """Prefer a recent round trip timed through the worker tunnel over the heartbeat's HTTP round trip."""
    hit = (path_rtt or {}).get(device_id)
    if hit and time.monotonic() - hit[1] <= PATH_RTT_MAX_AGE_S:
        return hit[0], "tunnel"
    return heartbeat_rtt, "heartbeat"


def _profiles(store: Store, group_id: str, settings: Settings, path_rtt: dict | None = None) -> list[DeviceProfile]:
    out = []
    for d in store.all("SELECT * FROM devices WHERE group_id = ?", (group_id,)):
        cap, pol = json.loads(d["capabilities"]), json.loads(d["policy"])
        fresh = bool(d["last_seen"]) and (now() - parse(d["last_seen"])).total_seconds() <= settings.heartbeat_unassign_s
        worker = cap.get("worker")
        reason = None if fresh else "device not ready (no valid heartbeat)"
        if fresh and worker and worker["state"] != "running":
            reason = f"inference worker stopped: {worker.get('last_stop_reason') or 'not started'}"
        bandwidth, source = cap["mem_bandwidth_gbps"], "reported"
        rtt, rtt_source = _planning_rtt(path_rtt, d["id"], cap["rtt_ms"])
        probe = cap.get("decode_probe")
        if probe and probe["model"] in CATALOG:  # convert timed decoding into streamed bytes per second
            ref = CATALOG[probe["model"]]
            bandwidth = round((ref.layers * ref.bytes_per_layer + ref.head_bytes) * probe["tokens_per_s"] / 1e9, 1)
            source = "decode_probe"
        out.append(DeviceProfile(
            device_id=d["id"], name=d["name"], os=cap["os"], arch=cap["arch"], backend_digest=cap["backend_digest"],
            formats=tuple(cap["formats"]), budget_bytes=min(pol["memory_cap_bytes"], cap["allocatable_bytes"]),
            mem_bandwidth_gbps=bandwidth, rtt_ms=rtt, rtt_source=rtt_source, power=cap["power"],
            allow_battery=pol["allow_battery"], ready=reason is None, authorized=d["status"] == "active",
            paused=pol["paused"] or pol.get("reported_paused", False), stability=1.0,
            endpoint=worker["endpoint"] if worker and worker["state"] == "running" else None,
            bandwidth_source=source, not_ready_reason=reason, node_state=cap.get("node_state") if fresh else None))
    return out


def _sse(event: dict) -> str:
    return f"id: {event['sequence']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"


def register(app: FastAPI) -> None:
    settings: Settings = app.state.settings

    # --- Groups and authentication ---------------------------------------------------------
    @app.post("/api/v1/groups", status_code=201, tags=["groups"])
    async def create_group(body: CreateGroupIn, request: Request):
        """Local bootstrap only: the coordinator host creates the group (FR 01)."""
        if (request.client.host if request.client else "") not in LOOPBACK:
            raise ApiError(403, "BOOTSTRAP_LOCAL_ONLY", "Create groups from the coordinator computer.")
        store = S(request)
        gid, mid, recovery = new_id("grp"), new_id("mem"), new_secret(18)
        fp = fingerprint(os.urandom(32))
        pw = await run_in_threadpool(request.app.state.passwords.hash, body.passphrase)
        with store.tx() as c:
            c.execute("INSERT INTO groups (id, display_name, fingerprint, created_at) VALUES (?,?,?,?)",
                      (gid, body.name, fp, iso(now())))
            c.execute("INSERT INTO members VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (mid, gid, body.owner_login, body.owner_display_name, pw, verifier(recovery), "owner", "active",
                       iso(now()), None))
            store.audit(c, gid, mid, "group.create", gid)
        return {"group_id": gid, "member_id": mid, "fingerprint": fp, "recovery_code": recovery}

    @app.post("/api/v1/auth/login", tags=["auth"])
    async def login(body: LoginIn, request: Request, response: Response):
        store, key = S(request), (body.group_id, body.login)
        attempts = [t for t in request.app.state.login_attempts.get(key, []) if t > time.time() - settings.login_window_s]
        if len(attempts) >= settings.login_attempts_per_window:
            raise ApiError(429, "RATE_LIMITED", "Too many sign-in attempts. Try again later.", True,
                           headers={"Retry-After": str(settings.login_window_s)})
        m = store.one("SELECT * FROM members WHERE group_id = ? AND login = ? AND status = 'active'", key)
        ok = await run_in_threadpool(request.app.state.passwords.verify, m["pass_hash"] if m else None, body.passphrase)
        if not ok:
            request.app.state.login_attempts[key] = attempts + [time.time()]
            raise ApiError(401, "INVALID_LOGIN", "The group, login, or passphrase is incorrect.")
        request.app.state.login_attempts.pop(key, None)
        sid, csrf = new_secret(), new_secret(16)
        with store.tx() as c:
            c.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (verifier(sid), m["id"], csrf, iso(now()), iso(now())))
            store.audit(c, m["group_id"], m["id"], "auth.login")
        response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict", secure=settings.cookie_secure,
                            max_age=settings.session_absolute_s, path="/")
        return {"member_id": m["id"], "group_id": m["group_id"], "role": m["role"], "csrf_token": csrf}

    @app.post("/api/v1/auth/logout", status_code=204, tags=["auth"])
    async def logout(request: Request, response: Response, p: Principal = Depends(current)):
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            with S(request).tx() as c:
                c.execute("DELETE FROM sessions WHERE verifier = ?", (verifier(sid),))
        response.delete_cookie(SESSION_COOKIE, path="/")

    @app.get("/api/v1/auth/session", tags=["auth"])
    async def session(request: Request, p: Principal = Depends(current)):
        m = S(request).one("SELECT display_name, login FROM members WHERE id = ?", (p.member_id,))
        g = S(request).one("SELECT display_name, fingerprint FROM groups WHERE id = ?", (p.group_id,))
        return {"member_id": p.member_id, "group_id": p.group_id, "role": p.role.value, "via": p.via,
                "display_name": m["display_name"], "login": m["login"], "group_name": g["display_name"],
                "group_fingerprint": g["fingerprint"]}

    @app.post("/api/v1/auth/recover", tags=["auth"])
    async def recover(body: RecoverIn, request: Request):
        """Rotates credentials and revokes sessions and API keys; issues a new recovery code (spec 6.5)."""
        store = S(request)
        m = store.one("SELECT * FROM members WHERE group_id = ? AND login = ? AND status = 'active'",
                      (body.group_id, body.login))
        if not m or not matches(body.recovery_code, m["recovery_verifier"]):
            raise ApiError(401, "INVALID_RECOVERY", "The recovery details are incorrect.")
        pw, new_code = await run_in_threadpool(request.app.state.passwords.hash, body.new_passphrase), new_secret(18)
        with store.tx() as c:
            c.execute("UPDATE members SET pass_hash = ?, recovery_verifier = ? WHERE id = ?", (pw, verifier(new_code), m["id"]))
            c.execute("DELETE FROM sessions WHERE member_id = ?", (m["id"],))
            c.execute("UPDATE credentials SET revoked_at = ? WHERE member_id = ? AND revoked_at IS NULL", (iso(now()), m["id"]))
            store.audit(c, m["group_id"], m["id"], "auth.recover")
        return {"recovery_code": new_code}

    # --- Invitations and membership ---------------------------------------------------------
    @app.post("/api/v1/groups/{group_id}/invites", status_code=201, tags=["groups"])
    async def create_invite(group_id: str, body: InviteIn, request: Request, p: Principal = Depends(current)):
        require_group(p, group_id)
        require_admin(p)
        iid, secret = new_id("inv"), new_secret()
        expires = now() + timedelta(hours=body.ttl_hours)
        with S(request).tx() as c:
            c.execute("INSERT INTO invitations (id, group_id, verifier, role, issuer_id, expires_at) VALUES (?,?,?,?,?,?)",
                      (iid, group_id, verifier(secret), body.role, p.member_id, iso(expires)))
            S(request).audit(c, group_id, p.member_id, "invite.create", iid)
        return {"invitation_id": iid, "code": f"{iid}.{secret}", "expires_at": iso(expires)}

    @app.delete("/api/v1/groups/{group_id}/invites/{invitation_id}", status_code=204, tags=["groups"])
    async def revoke_invite(group_id: str, invitation_id: str, request: Request, p: Principal = Depends(current)):
        require_group(p, group_id)
        require_admin(p)
        with S(request).tx() as c:
            c.execute("UPDATE invitations SET revoked_at = ? WHERE id = ? AND group_id = ? AND revoked_at IS NULL",
                      (iso(now()), invitation_id, group_id))

    def _valid_invite(store: Store, code: str):
        iid, _, secret = code.partition(".")
        inv = store.one("SELECT * FROM invitations WHERE id = ?", (iid,))
        if (not inv or not secret or not matches(secret, inv["verifier"]) or inv["redeemed_at"] or inv["revoked_at"]
                or parse(inv["expires_at"]) <= now()):
            raise ApiError(404, "INVITATION_INVALID", "This invitation is invalid, expired, or already used.")
        return inv

    @app.post("/api/v1/invitations/inspect", tags=["groups"])
    async def inspect_invite(body: CodeIn, request: Request):
        """Shows the group name and fingerprint before joining (FR 03). Discloses no content."""
        inv = _valid_invite(S(request), body.code)
        g = S(request).one("SELECT display_name, fingerprint FROM groups WHERE id = ?", (inv["group_id"],))
        return {"group_name": g["display_name"], "group_fingerprint": g["fingerprint"], "role": inv["role"],
                "expires_at": inv["expires_at"],
                "privacy_notice": "Private conversations are hidden from other members in the app, but the "
                                  "computers that run the model, and their administrators, can see what they process."}

    @app.post("/api/v1/invitations/redeem", status_code=201, tags=["groups"])
    async def redeem(body: RedeemIn, request: Request):
        store = S(request)
        inv = _valid_invite(store, body.code)
        pw, recovery, mid = await run_in_threadpool(request.app.state.passwords.hash, body.passphrase), new_secret(18), new_id("mem")
        with store.tx() as c:
            # Transactional single use: only one redemption can flip redeemed_at (FR 02).
            cur = c.execute("UPDATE invitations SET redeemed_at = ?, redeemed_by = ? WHERE id = ? AND redeemed_at IS NULL "
                            "AND revoked_at IS NULL AND expires_at > ?", (iso(now()), mid, inv["id"], iso(now())))
            if cur.rowcount != 1:
                raise ApiError(404, "INVITATION_INVALID", "This invitation is invalid, expired, or already used.")
            if c.execute("SELECT 1 FROM members WHERE group_id = ? AND login = ?", (inv["group_id"], body.login)).fetchone():
                raise ApiError(409, "LOGIN_TAKEN", "That login is already used in this group.")
            c.execute("INSERT INTO members VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (mid, inv["group_id"], body.login, body.display_name, pw, verifier(recovery), inv["role"],
                       "active", iso(now()), None))
            store.audit(c, inv["group_id"], mid, "invite.redeem", inv["id"])
        g = store.one("SELECT fingerprint FROM groups WHERE id = ?", (inv["group_id"],))
        return {"group_id": inv["group_id"], "member_id": mid, "group_fingerprint": g["fingerprint"],
                "recovery_code": recovery}

    @app.get("/api/v1/groups/{group_id}/members", tags=["groups"])
    async def list_members(group_id: str, request: Request, p: Principal = Depends(current)):
        require_group(p, group_id)
        rows = S(request).all("SELECT id, display_name, role, status FROM members WHERE group_id = ? AND status = 'active'",
                              (group_id,))
        return {"members": [{"member_id": r["id"], "display_name": r["display_name"], "role": r["role"]} for r in rows]}

    @app.delete("/api/v1/groups/{group_id}/members/{member_id}", status_code=204, tags=["groups"])
    async def remove_member(group_id: str, member_id: str, request: Request, p: Principal = Depends(current)):
        """Revokes sessions, credentials, and devices and cancels queued and running work (FR 04)."""
        require_group(p, group_id)
        require_admin(p)
        store = S(request)
        m = store.one("SELECT * FROM members WHERE id = ? AND group_id = ? AND status = 'active'", (member_id, group_id))
        if not m:
            raise not_found()
        if m["role"] == "owner":
            raise ApiError(409, "OWNER_REMOVAL", "Transfer ownership before removing the owner.")
        with store.tx() as c:
            c.execute("UPDATE members SET status = 'revoked', revoked_at = ? WHERE id = ?", (iso(now()), member_id))
            c.execute("DELETE FROM sessions WHERE member_id = ?", (member_id,))
            c.execute("UPDATE credentials SET revoked_at = ? WHERE member_id = ? AND revoked_at IS NULL", (iso(now()), member_id))
            c.execute("UPDATE devices SET status = 'revoked' WHERE owner_id = ?", (member_id,))
            store.audit(c, group_id, p.member_id, "member.remove", member_id)
        request.app.state.scheduler.cancel_member(group_id, member_id)

    # --- Application credentials -------------------------------------------------------------
    @app.post("/api/v1/credentials", status_code=201, tags=["credentials"])
    async def create_credential(body: CredentialIn, request: Request, p: Principal = Depends(current)):
        require_session(p)  # fresh, interactive authentication path (spec 6.5)
        cid, secret = new_id("key"), "difa_" + new_secret(32)
        expires = now() + timedelta(days=body.expires_days)
        if "metrics" in body.scopes:  # a dashboard key: read-only metrics, nothing else (S3.6)
            require_admin(p)
            if set(body.scopes) != {"metrics"}:
                raise ApiError(400, "INVALID_FIELDS", "A metrics key carries the metrics scope only.")
            scopes = ["metrics"]
        else:
            scopes = sorted(set(body.scopes) | {"inference"})
        with S(request).tx() as c:
            c.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?)",
                      (cid, p.member_id, p.group_id, body.name, verifier(secret), ",".join(scopes), iso(expires), None,
                       iso(now())))
            S(request).audit(c, p.group_id, p.member_id, "credential.create", cid)
        return {"credential_id": cid, "secret": secret, "scopes": scopes, "expires_at": iso(expires)}

    @app.get("/api/v1/credentials", tags=["credentials"])
    async def list_credentials(request: Request, p: Principal = Depends(current)):
        rows = S(request).all("SELECT id, name, scopes, expires_at, revoked_at FROM credentials WHERE member_id = ?",
                              (p.member_id,))
        return {"credentials": [{"credential_id": r["id"], "name": r["name"], "scopes": r["scopes"].split(","),
                                 "expires_at": r["expires_at"], "revoked": bool(r["revoked_at"])} for r in rows]}

    @app.delete("/api/v1/credentials/{credential_id}", status_code=204, tags=["credentials"])
    async def revoke_credential(credential_id: str, request: Request, p: Principal = Depends(current)):
        row = S(request).one("SELECT member_id, group_id FROM credentials WHERE id = ?", (credential_id,))
        if not row or row["group_id"] != p.group_id or (row["member_id"] != p.member_id and not p.is_admin):
            raise not_found()
        with S(request).tx() as c:
            c.execute("UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (iso(now()), credential_id))

    # --- Devices ------------------------------------------------------------------------------
    @app.post("/api/v1/devices/enrollment-codes", status_code=201, tags=["devices"])
    async def enrollment_code(request: Request, p: Principal = Depends(current)):
        """One-time code a member gives to the agent on their own device (spec 6.3 headless flow)."""
        require_session(p)
        eid, secret = new_id("enr"), new_secret()
        expires = now() + timedelta(minutes=10)
        with S(request).tx() as c:
            c.execute("INSERT INTO enrollment_codes VALUES (?,?,?,?,?,?)",
                      (eid, p.group_id, p.member_id, verifier(secret), iso(expires), None))
        out = {"code": f"{eid}.{secret}", "expires_at": iso(expires)}
        if request.app.state.control_fingerprint:
            out["coordinator_fingerprint"] = request.app.state.control_fingerprint
        return out

    @app.post("/api/v1/devices/enroll", status_code=201, tags=["devices"])
    async def enroll(body: EnrollIn, request: Request):
        store = S(request)
        eid, _, secret = body.code.partition(".")
        row = store.one("SELECT * FROM enrollment_codes WHERE id = ?", (eid,))
        if not row or not secret or not matches(secret, row["verifier"]) or row["used_at"] or parse(row["expires_at"]) <= now():
            raise ApiError(404, "ENROLLMENT_INVALID", "This enrollment code is invalid, expired, or already used.")
        did, token = new_id("dev"), new_secret(32)
        policy = body.policy.model_dump()
        has_cert = is_certificate(body.public_key)
        if body.public_key.startswith("-----BEGIN") and not has_cert:
            raise ApiError(400, "INVALID_DEVICE_KEY", "The device certificate could not be parsed.")
        with store.tx() as c:
            cur = c.execute("UPDATE enrollment_codes SET used_at = ? WHERE id = ? AND used_at IS NULL", (iso(now()), eid))
            if cur.rowcount != 1:
                raise ApiError(404, "ENROLLMENT_INVALID", "This enrollment code is invalid, expired, or already used.")
            c.execute("INSERT INTO devices (id, group_id, owner_id, name, public_key, token_verifier, capabilities, policy,"
                      " status, enrolled_at, last_seen, consent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (did, row["group_id"], row["member_id"], body.name, body.public_key, verifier(token),
                       body.capabilities.model_dump_json(), json.dumps(policy), "active", iso(now()), iso(now()),
                       body.consent.model_dump_json() if body.consent else None))
            store.audit(c, row["group_id"], row["member_id"], "device.enroll", did)
        with obs.bound(node_id=did):
            obs.log(log, logging.INFO, "node enrolled", os=body.capabilities.os, arch=body.capabilities.arch,
                    tunnel_certificate=has_cert)
        return {"device_id": did, "device_token": token, "group_id": row["group_id"],
                "heartbeat_interval_s": settings.heartbeat_interval_s, "lease_s": 30,
                "coordinator_cert_pem": request.app.state.tunnels.identity.cert_pem,
                "device_cert_sha256": cert_fingerprint(body.public_key) if has_cert else None}

    _device_by_token = device_by_token

    @app.post("/api/v1/devices/{device_id}/heartbeat", tags=["devices"])
    async def heartbeat(device_id: str, body: HeartbeatIn, request: Request):
        """Agents report capabilities and their locally enforced state; the response renews the lease."""
        d = _device_by_token(request, device_id)
        policy = json.loads(d["policy"])
        policy["reported_paused"] = body.paused
        caps = body.capabilities.model_dump()
        if body.worker:
            caps["worker"] = body.worker.model_dump()
        if body.prefetch:
            caps["prefetch"] = body.prefetch.model_dump()
        if body.node_state:
            caps["node_state"] = body.node_state
        if body.telemetry:
            caps["telemetry"] = body.telemetry.model_dump()
        if body.contribution:
            caps["contribution"] = body.contribution.model_dump()
        if body.log_level:
            caps["log_level"] = body.log_level
        consent = body.consent.model_dump_json() if body.consent else d["consent"]
        if body.consent and consent != d["consent"]:
            obs.log(log, logging.INFO, "consent changed", idle_data=body.consent.idle_data,
                    diagnostics=body.consent.diagnostics, policy_version=body.consent.policy_version)
        with S(request).tx() as c:
            c.execute("UPDATE devices SET capabilities = ?, policy = ?, last_seen = ?, consent = ? WHERE id = ?",
                      (json.dumps(caps), json.dumps(policy), iso(now()), consent, device_id))
        g = request.app.state.scheduler.group(d["group_id"])
        stage = next((s for s in (g.placement or {}).get("stages", []) if s["device_id"] == device_id), None)
        public_policy = {k: v for k, v in policy.items() if k != "reported_paused"}
        # A worker without a decode probe yet is told where to get the reference model (S5.6).
        offer = None
        if body.worker and body.capabilities.decode_probe is None:
            offer = await run_in_threadpool(probe_offer, request.app)
            offer = offer and {**offer, "url": offer["url"].format(device_id=device_id)}
        pending = request.app.state.prefetch.get(d["group_id"])
        manifest = pending["manifests"].get(device_id) if pending else None
        return {"policy": public_policy, "policy_rev": d["policy_rev"], "lease_s": 30,
                "assignment": {"deployment_id": g.deployment_id, "layers": stage["layers"]} if stage else None,
                # Lets the agent tag its own and its worker's log lines with the requests it is serving (D-11).
                "active_request_ids": sorted(g.running) if stage else [],
                "prefetch": {"deployment_id": pending["deployment_id"], **manifest} if manifest else None,
                "decode_probe_model": offer,
                "telemetry": {"interval_s": settings.telemetry_interval_s, "idle_after_s": settings.idle_after_s},
                # Contribution governor settings (S4.9): the agent applies them locally, under its owner's ceiling.
                "contribution": {"idle_after_s": settings.idle_after_s, "ramp_s": settings.ramp_s,
                                 "yield_level": settings.yield_level, "max_cpu_fraction": settings.max_cpu_fraction,
                                 "windows": settings.contribution_windows},
                **diagnostics.heartbeat_extras(S(request), d)}

    @app.post("/api/v1/devices/{device_id}/telemetry", tags=["devices"])
    async def upload_telemetry(device_id: str, body: TelemetryBatchIn, request: Request):
        """Idle samples, state changes, health and error counts from an agent's on-disk buffer (S2.3, S2.4,
        S3.5). Repeats are ignored. Each record kind needs its own consent: idle data or diagnostics."""
        d = _device_by_token(request, device_id)
        kinds = {r.kind for r in body.records} or {"idle"}
        if not telemetry.has_consent(json.loads(d["consent"]) if d["consent"] else None, kinds):
            what = ("diagnostics" if telemetry.has_consent(json.loads(d["consent"]) if d["consent"] else None,
                                                           kinds & telemetry.IDLE_KINDS) else "idle-data collection")
            raise ApiError(403, "TELEMETRY_NO_CONSENT", f"The device owner has not consented to {what}.")
        node = request.app.state.node_keys.key(device_id)
        return await run_in_threadpool(telemetry.ingest, S(request), node, body, settings.org_id, settings.site_id,
                                       request.app.state.node_keys.key)

    @app.get("/api/v1/groups/{group_id}/devices/{device_id}/idle", tags=["devices"])
    async def idle_timeline(group_id: str, device_id: str, request: Request, hours: float = 24,
                            p: Principal = Depends(current)):
        """A device's idle samples and state changes, for its owner or a group administrator."""
        require_group(p, group_id)
        d = S(request).one("SELECT owner_id, name FROM devices WHERE id = ? AND group_id = ?", (device_id, group_id))
        if not d or (d["owner_id"] != p.member_id and not p.is_admin):
            raise not_found()
        hours = min(max(hours, 0.1), 24 * 90)
        node = request.app.state.node_keys.key(device_id)
        out = telemetry.timeline(S(request), node, now() - timedelta(hours=hours))
        return {"device_id": device_id, "name": d["name"], "hours": hours, **out}

    @app.get("/api/v1/groups/{group_id}/network", tags=["devices"])
    async def network(group_id: str, request: Request, p: Principal = Depends(current)):
        """The network graph of the group's devices: measured links, whether each link is fast enough to
        join a cluster, and each device's cluster (S5.4). Group administrators only."""
        require_group(p, group_id)
        require_admin(p)
        keys = request.app.state.node_keys
        devices = {keys.key(r["id"]): {"device_id": r["id"], "name": r["name"]} for r in S(request).all(
            "SELECT id, name FROM devices WHERE group_id = ? AND status != 'revoked'", (group_id,))}
        return await run_in_threadpool(graph.network_view, S(request), devices, cluster_policy(settings),
                                       settings.cluster_max_age_h)

    @app.get("/api/v1/devices/{device_id}/model-file", tags=["devices"])
    async def model_file(device_id: str, request: Request):
        """Byte ranges of the model a device is assigned to prefetch (FR 30), for LAN or test use."""
        d = _device_by_token(request, device_id)
        pending = request.app.state.prefetch.get(d["group_id"])
        if not pending or device_id not in pending["manifests"]:
            raise ApiError(404, "NO_PREFETCH", "This device has no partition to download.")
        man = pending["manifests"][device_id]
        allowed = [(t["offset"], t["offset"] + t["nbytes"]) for t in man["tensors"]]
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("range", ""))
        if not m:
            raise ApiError(416, "RANGE_REQUIRED", "Request one assigned byte range.")
        start, end = int(m.group(1)), int(m.group(2)) + 1
        if not any(a <= start and end <= b for a, b in allowed):  # only this device's own ranges
            raise ApiError(403, "RANGE_NOT_ASSIGNED", "That range is not part of this device's partition.")
        path = request.app.state.runtime.model_path(man["model"])

        def chunks():
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start
                while left:
                    data = f.read(min(left, 1 << 20))
                    if not data:
                        return
                    left -= len(data)
                    yield data
        return StreamingResponse(chunks(), status_code=206, media_type="application/octet-stream",
                                 headers={"Content-Range": f"bytes {start}-{end - 1}/{path.stat().st_size}",
                                          "Content-Length": str(end - start)})

    @app.get("/api/v1/devices/{device_id}/probe-model", tags=["devices"])
    async def probe_model(device_id: str, request: Request):
        """The decode-probe reference model (S5.6), whole, for an enrolled device over the pinned channel."""
        _device_by_token(request, device_id)
        offer = await run_in_threadpool(probe_offer, request.app)
        if not offer:
            raise ApiError(404, "PROBE_MODEL_UNAVAILABLE", "This coordinator has no decode-probe model to serve.")
        path = request.app.state.runtime.model_path(PROBE_MODEL)

        def chunks():
            with open(path, "rb") as f:
                while data := f.read(1 << 20):
                    yield data
        return StreamingResponse(chunks(), media_type="application/octet-stream",
                                 headers={"Content-Length": str(offer["nbytes"])})

    @app.patch("/api/v1/devices/{device_id}/policy", tags=["devices"])
    async def update_policy(device_id: str, body: PolicyPatch, request: Request, p: Principal = Depends(current)):
        """Owners set limits; administrators may only disable. Agents also apply local ceilings (FR 07)."""
        store = S(request)
        d = store.one("SELECT * FROM devices WHERE id = ? AND group_id = ?", (device_id, p.group_id))
        if not d:
            raise not_found()
        changes = body.model_dump(exclude_none=True)
        is_owner = d["owner_id"] == p.member_id
        if not is_owner:
            if not p.is_admin or set(changes) - {"disabled"}:
                raise ApiError(403, "FORBIDDEN", "Only the device owner can change its contribution limits.")
        status = d["status"]
        if changes.pop("disabled", None):
            status = "disabled"
        policy = {**json.loads(d["policy"]), **changes}
        with store.tx() as c:
            c.execute("UPDATE devices SET policy = ?, policy_rev = policy_rev + 1, status = ? WHERE id = ?",
                      (json.dumps(policy), status, device_id))
            store.audit(c, p.group_id, p.member_id, "device.policy", device_id)
        return {"device_id": device_id, "status": status, "policy": {k: v for k, v in policy.items() if k != "reported_paused"}}

    @app.get("/api/v1/groups/{group_id}/devices", tags=["devices"])
    async def list_devices(group_id: str, request: Request, p: Principal = Depends(current)):
        require_group(p, group_id)
        out = []
        hardware = {d["id"]: json.loads(d["capabilities"]).get("hardware")
                    for d in S(request).all("SELECT id, capabilities FROM devices WHERE group_id = ?", (group_id,))}
        for prof in _profiles(S(request), group_id, settings, request.app.state.path_rtt):
            state = ("unavailable" if not prof.ready else "paused" if prof.paused else
                     "incompatible" if prof.backend_digest != request.app.state.runtime.backend_digest else "ready")
            out.append({"device_id": prof.device_id, "name": prof.name, "os": prof.os, "arch": prof.arch,
                        "state": state, "budget_bytes": prof.budget_bytes, "power": prof.power,
                        "worker_running": prof.endpoint is not None, "node_state": prof.node_state,
                        "hardware": hardware.get(prof.device_id)})
        return {"devices": out}

    # --- Deployments ------------------------------------------------------------------------
    @app.post("/api/v1/groups/{group_id}/deployment", tags=["deployments"])
    async def deploy(group_id: str, body: DeploymentIn, request: Request, p: Principal = Depends(current)):
        """Plan with the filter-and-score pipeline, then load (FR 13, FR 14, FR 31)."""
        require_group(p, group_id)
        require_admin(p)
        model = CATALOG.get(body.model)
        if not model:
            raise ApiError(400, "UNKNOWN_MODEL", "That model is not in the group's catalog.")
        sched = request.app.state.scheduler
        g = sched.group(group_id)
        if g.running or len(g.queue):
            raise ApiError(409, "DEPLOYMENT_BUSY", "Wait for queued and running requests to finish.", True)
        policy = PlacementPolicy(backend_digest=request.app.state.runtime.backend_digest, require_all=body.require_all,
                                 local_device=getattr(request.app.state.runtime, "local_device", None))
        explanation = plan(_profiles(S(request), group_id, settings, request.app.state.path_rtt), model, policy)
        if not explanation["feasible"]:
            raise ApiError(409, "PLACEMENT_INFEASIBLE", explanation.get("reason", "No feasible placement."),
                           extra={"explanation": explanation})
        dep_id = new_id("dep")
        if g.deployment_id:  # release the previous backend process before loading a new revision
            await unload(request.app, g.deployment_id)
            sched.set_deployment(group_id, None, None)
        with S(request).tx() as c:
            c.execute("UPDATE deployments SET state = 'retired' WHERE group_id = ? AND state != 'retired'", (group_id,))
            c.execute("INSERT INTO deployments VALUES (?,?,?,?,?,?,?)",
                      (dep_id, group_id, model.name, policy.backend_digest, json.dumps(explanation), "loading", iso(now())))
        request.app.state.prefetch.pop(group_id, None)
        request.app.state.prefetch_errors.pop(group_id, None)
        if body.prefetch:
            return await _start_prefetch(request, group_id, dep_id, model.name, explanation, p.member_id)
        try:
            loaded = await load_now(request.app, group_id, dep_id, explanation, p.member_id)
        except RuntimeFailure as e:
            raise ApiError(503 if e.retryable else 409, e.code, e.message, e.retryable)
        return {"deployment_id": dep_id, "model": model.name, "state": "ready", "loaded": loaded,
                "explanation": explanation}

    async def _start_prefetch(request: Request, group_id: str, dep_id: str, model_name: str, explanation: dict,
                              member_id: str):
        runtime, store = request.app.state.runtime, S(request)
        meta = MODEL_FILES.get(model_name)
        if not hasattr(runtime, "stage_tensors") or not meta:
            _fail_deployment(store, group_id, dep_id, "PREFETCH_UNSUPPORTED")
            raise ApiError(409, "PREFETCH_UNSUPPORTED", "This backend or model cannot prefetch partitions.")
        local = getattr(runtime, "local_device", None)
        url = (f"/api/v1/devices/{{device_id}}/model-file" if settings.model_source == "coordinator"
               else meta.get("url"))
        try:
            manifests = await run_in_threadpool(partition.build, runtime, model_name, meta, explanation["stages"], url)
        except (RuntimeFailure, ValueError) as e:
            _fail_deployment(store, group_id, dep_id, "PREFETCH_UNSUPPORTED", str(e))
            raise ApiError(409, "PREFETCH_UNSUPPORTED", getattr(e, "message", str(e)))
        remote = {st["device_id"] for st in explanation["stages"] if st["name"] != local}
        manifests = {k: {**v, "url": v["url"].replace("{device_id}", k)} for k, v in manifests.items() if k in remote}
        with store.tx() as c:
            c.execute("UPDATE deployments SET state = 'preparing' WHERE id = ?", (dep_id,))
            store.audit(c, group_id, member_id, "deployment.prefetch", dep_id)
        request.app.state.prefetch[group_id] = {"deployment_id": dep_id, "explanation": explanation,
                                                "manifests": manifests, "member_id": member_id,
                                                "started": time.monotonic()}
        summary = [{"device_id": k, "tensors": len(v["tensors"]), "bytes_prefetch": v["bytes_prefetch"],
                    "bytes_stage": v["bytes_stage"]} for k, v in manifests.items()]
        return JSONResponse({"deployment_id": dep_id, "model": model_name, "state": "preparing",
                             "prefetch": summary, "explanation": explanation}, status_code=202)

    @app.get("/api/v1/groups/{group_id}/deployment", tags=["deployments"])
    async def get_deployment(group_id: str, request: Request, p: Principal = Depends(current)):
        require_group(p, group_id)
        row = S(request).one("SELECT * FROM deployments WHERE group_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                             (group_id,))
        if not row:
            return {"state": "unavailable", "deployment_id": None}
        out = {"deployment_id": row["id"], "model": row["model"], "state": row["state"]}
        pending = request.app.state.prefetch.get(group_id)
        if pending and pending["deployment_id"] == row["id"]:
            out["prefetch"] = _prefetch_progress(S(request), pending)
        err = request.app.state.prefetch_errors.get(group_id)
        if err and row["state"] == "failed":
            out["error"] = err
        if p.is_admin:
            out["explanation"] = json.loads(row["placement"])
        return out

    @app.get("/api/v1/groups/{group_id}/queue", tags=["deployments"])
    async def queue_view(group_id: str, request: Request, p: Principal = Depends(current)):
        """Content-free queue view for administrators: positions and states, never prompts."""
        require_group(p, group_id)
        require_admin(p)
        g = request.app.state.scheduler.group(group_id)
        names = {r["id"]: r["display_name"] for r in S(request).all("SELECT id, display_name FROM members WHERE group_id = ?",
                                                                     (group_id,))}
        items = [{"position": i + 1, "request_id": rid, "member": names.get(g.queue._owner.get(rid)), "state": "queued"}
                 for i, rid in enumerate(g.queue.service_order())]
        running = [{"request_id": rid, "state": "running"} for rid in g.running]
        return {"running": running, "queued": items}

    # --- Conversations ----------------------------------------------------------------------
    def _branch_out(row) -> dict:
        return {"branch_id": row["id"], "visibility": row["visibility"], "title": row["title"],
                "owner_id": row["owner_id"], "source_branch_id": row["source_branch_id"], "created_at": row["created_at"]}

    @app.post("/api/v1/conversations", status_code=201, tags=["conversations"])
    async def create_conversation(body: ConversationIn, request: Request, p: Principal = Depends(current)):
        if not can_create_with_visibility(p, body.visibility):
            raise ApiError(403, "FORBIDDEN", "This credential cannot create shared conversations.")
        bid = new_id("br")
        retention = S(request).one("SELECT retention_days FROM groups WHERE id = ?", (p.group_id,))["retention_days"]
        with S(request).tx() as c:
            S(request).create_branch(c, bid, p.group_id, p.member_id, body.visibility.value,
                                     body.title or "New conversation", retention)
        return _branch_out(S(request).one("SELECT * FROM branches WHERE id = ?", (bid,)))

    @app.get("/api/v1/conversations", tags=["conversations"])
    async def list_conversations(request: Request, p: Principal = Depends(current)):
        rows = S(request).all("SELECT * FROM branches WHERE group_id = ? AND deleted_at IS NULL AND "
                              "(visibility = 'group' OR owner_id = ?) ORDER BY created_at DESC", (p.group_id, p.member_id))
        return {"conversations": [_branch_out(r) for r in rows if can_read_branch(p, _branch(r))]}

    @app.get("/api/v1/conversations/{branch_id}", tags=["conversations"])
    async def get_conversation(branch_id: str, request: Request, p: Principal = Depends(current)):
        row = _load_branch(S(request), p, branch_id)
        msgs = [{"message_id": m["id"], "role": m["role"], "content": m["content"], "completion_state": m["completion_state"],
                 "created_at": m["created_at"]} for m in S(request).branch_messages(branch_id)]
        return {**_branch_out(row), "messages": msgs}

    @app.delete("/api/v1/conversations/{branch_id}", status_code=204, tags=["conversations"])
    async def delete_conversation(branch_id: str, request: Request, p: Principal = Depends(current)):
        row = _load_branch(S(request), p, branch_id)
        if not can_delete_branch(p, _branch(row)):
            raise ApiError(403, "FORBIDDEN", "Only the conversation owner can delete it.")
        with S(request).tx() as c:
            c.execute("UPDATE branches SET deleted_at = ? WHERE id = ?", (iso(now()), branch_id))
            c.execute("DELETE FROM messages WHERE branch_id = ?", (branch_id,))  # purge live content
            S(request).audit(c, p.group_id, p.member_id, "conversation.delete", branch_id)

    @app.post("/api/v1/conversations/{branch_id}/fork", status_code=201, tags=["conversations"])
    async def fork(branch_id: str, body: ForkIn, request: Request, p: Principal = Depends(current)):
        """Visibility changes create a new branch with immutable visibility (FR 18)."""
        store = S(request)
        row = _load_branch(store, p, branch_id)
        source_msgs = store.branch_messages(branch_id)
        try:
            copy_ids = plan_fork(p, _branch(row), body.visibility, [m["id"] for m in source_msgs],
                                 body.include_message_ids, body.include_all, body.preview_confirmed)
        except ForkError as e:
            status = {"NOT_FOUND": 404, "FORBIDDEN": 403}.get(e.code, 409)
            raise ApiError(status, e.code, str(e))
        by_id = {m["id"]: m for m in source_msgs}
        bid = new_id("br")
        # Titles can carry private text (they default to the first message), so a widening fork
        # never inherits one; the user names the shared branch explicitly or gets a neutral title.
        widening = row["visibility"] == Visibility.PRIVATE.value and body.visibility is Visibility.GROUP
        title = body.title or ("Shared conversation" if widening else row["title"])
        retention = store.one("SELECT retention_days FROM groups WHERE id = ?", (p.group_id,))["retention_days"]
        with store.tx() as c:
            store.create_branch(c, bid, p.group_id, p.member_id, body.visibility.value,
                                title, retention, source=branch_id)
            for mid in copy_ids:
                m = by_id[mid]
                store.add_message(c, new_id("msg"), bid, m["author_id"], m["role"], m["content"], m["completion_state"])
            store.audit(c, p.group_id, p.member_id, "conversation.fork", bid,
                        json.dumps({"source": branch_id, "copied": copy_ids, "visibility": body.visibility.value}))
        return {**_branch_out(store.one("SELECT * FROM branches WHERE id = ?", (bid,))), "copied_message_ids": copy_ids}

    # --- Inference requests -------------------------------------------------------------------
    @app.post("/api/v1/requests", status_code=202, tags=["requests"])
    async def create_request(body: RequestIn, request: Request, p: Principal = Depends(current)):
        return admit_request(request, p, body)

    @app.get("/api/v1/requests/{request_id}", tags=["requests"])
    async def get_request(request_id: str, request: Request, p: Principal = Depends(current)):
        r = S(request).one("SELECT * FROM requests WHERE id = ? AND member_id = ?", (request_id, p.member_id))
        if not r:
            raise not_found()
        out = request_status(request, r)
        return out

    @app.post("/api/v1/requests/{request_id}/cancel", tags=["requests"])
    async def cancel_request(request_id: str, request: Request, p: Principal = Depends(current)):
        r = S(request).one("SELECT * FROM requests WHERE id = ? AND group_id = ?", (request_id, p.group_id))
        if not r or (r["member_id"] != p.member_id and not p.is_admin):
            raise not_found()
        if RequestState(r["status"]) in TERMINAL:
            return {"request_id": request_id, "status": r["status"]}
        request.app.state.scheduler.cancel(p.group_id, request_id)
        await asyncio.sleep(0)
        return {"request_id": request_id, "status": "cancelling"}

    @app.get("/api/v1/requests/{request_id}/events", tags=["requests"])
    async def events(request_id: str, request: Request, p: Principal = Depends(current)):
        """Owner-scoped event stream with replay from Last-Event-ID (spec 9.3)."""
        r = S(request).one("SELECT * FROM requests WHERE id = ? AND member_id = ?", (request_id, p.member_id))
        ch = request.app.state.scheduler.channels.get(request_id)
        if not r or not ch:
            raise not_found()
        after = int(request.headers.get("last-event-id", "0") or 0)

        async def gen():
            async for ev in ch.iterate(after):
                yield _sse(ev)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def request_status(request: Request, r) -> dict:
    out = {"request_id": r["id"], "attempt": r["attempt"], "status": r["status"], "visibility": r["visibility"],
           "branch_id": r["branch_id"], "deployment_id": r["deployment_id"], "finish_reason": r["finish_reason"],
           "error_code": r["error_code"],
           "usage": {"prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"]}}
    if r["status"] == "queued":
        out["position"] = request.app.state.scheduler.position(r["group_id"], r["id"])
    return out


def admit_request(request: Request, p: Principal, body: RequestIn) -> dict:
    """Shared admission path for chat and API callers (FR 15, FR 22, FR 24, FR 25)."""
    store, sched, settings = S(request), request.app.state.scheduler, request.app.state.settings
    if body.model not in ("group-default",) and body.model not in CATALOG:
        raise ApiError(400, "UNKNOWN_MODEL", "Unknown model. Use group-default or a model from /v1/models.")
    g = sched.group(p.group_id)
    if not g.deployment_id:
        raise ApiError(503, "MODEL_UNAVAILABLE", "The model is not ready. Try again when it is.", True)
    if body.model != "group-default" and body.model != g.placement["model"]["name"]:
        raise ApiError(503, "MODEL_UNAVAILABLE", "That model is not the group's ready deployment.", True)
    if not can_create_with_visibility(p, body.visibility):
        raise ApiError(403, "FORBIDDEN", "This credential needs the share scope to create shared content.")

    payload_fp = hashlib.sha256(body.model_dump_json(exclude={"client_request_id"}).encode()).hexdigest()
    if body.client_request_id:
        prior = store.one("SELECT * FROM requests WHERE member_id = ? AND client_request_id = ? AND created_at > ?",
                          (p.member_id, body.client_request_id, iso(now() - timedelta(hours=24))))
        if prior:
            if prior["payload_fingerprint"] != payload_fp:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "This client_request_id was used with different content.")
            return _admission_out(prior)

    history, branch_id = [], body.branch_id
    if branch_id:
        row = _load_branch(store, p, branch_id)
        b = _branch(row)
        if b.visibility is not body.visibility:
            raise ApiError(409, "VISIBILITY_CONFLICT", "The request visibility does not match the conversation.")
        if not can_write_branch(p, b):
            raise ApiError(403, "SINGLE_WRITER", "Shared conversations have one writer; fork it to continue.")
        history = [m for m in store.branch_messages(branch_id) if m["completion_state"] == "complete"]

    new_msgs = [m.model_dump() for m in body.messages]
    used = context_tokens([{"content": m["content"]} for m in history] + new_msgs) + body.max_output_tokens
    if used > settings.max_context_tokens:
        raise ApiError(422, "CONTEXT_TOO_LONG",
                       f"Prompt, history, and reserved output need {used} tokens; the limit is "
                       f"{settings.max_context_tokens}. Start a new conversation or shorten the message.")
    try:
        sched.check_capacity(p.group_id, p.member_id)
    except QueueFull as e:
        msg = ("You already have the maximum number of waiting requests." if e.scope == "member"
               else "The group's queue is full.")
        raise ApiError(429, "QUEUE_FULL", msg, True, headers={"Retry-After": "10"})

    rid = new_id("req")
    retention = store.one("SELECT retention_days FROM groups WHERE id = ?", (p.group_id,))["retention_days"]
    with store.tx() as c:
        if not branch_id:
            branch_id = new_id("br")
            title = (new_msgs[-1]["content"][:60] or "New conversation")
            store.create_branch(c, branch_id, p.group_id, p.member_id, body.visibility.value, title, retention)
        ids = [m["id"] for m in history]
        for m in new_msgs:
            mid = new_id("msg")
            store.add_message(c, mid, branch_id, p.member_id, m["role"], m["content"])
            ids.append(mid)
        c.execute("INSERT INTO requests (id, group_id, member_id, branch_id, deployment_id, visibility, status, "
                  "context_message_ids, max_output_tokens, temperature, seed, client_request_id, payload_fingerprint, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, p.group_id, p.member_id, branch_id, g.deployment_id, body.visibility.value, "queued",
                   json.dumps(ids), body.max_output_tokens, body.temperature, body.seed, body.client_request_id,
                   payload_fp, iso(now()), iso(now())))
        store.audit(c, p.group_id, p.member_id, "request.admit", rid)
    sched.admit(p.group_id, p.member_id, rid)
    with obs.bound(request_id=rid, deployment_id=g.deployment_id):
        obs.log(log, logging.INFO, "request admitted", visibility=body.visibility.value,
                max_output_tokens=body.max_output_tokens, context_messages=len(ids))
    return _admission_out(store.one("SELECT * FROM requests WHERE id = ?", (rid,)))


def _admission_out(r) -> dict:
    return {"request_id": r["id"], "status": r["status"], "visibility": r["visibility"], "branch_id": r["branch_id"],
            "deployment_id": r["deployment_id"], "status_url": f"/api/v1/requests/{r['id']}",
            "events_url": f"/api/v1/requests/{r['id']}/events"}


def app_from_env() -> FastAPI:
    """The uvicorn factory: reads the site config (IDLEMESH_SITE_CONFIG), sets up JSON logging, builds the app."""
    from .. import site_config  # local import: tests build apps directly and need no YAML

    site = site_config.from_env()
    if site:
        obs.setup_logging(site.logging.level, site.log_file(), int(site.logging.max_mb * 1024 * 1024),
                          site.logging.keep)
        obs.set_site(site.site.org_id, site.site.site_id)
    else:
        obs.setup_logging("info")
    app = create_app(Settings.from_site(site) if site else None)
    app.state.site = site
    obs.log(log, logging.INFO, "coordinator starting", runtime=type(app.state.runtime).__name__,
            site_config=os.environ.get(site_config.ENV) is not None, telemetry_tier=site.telemetry.tier if site else None)
    return app
