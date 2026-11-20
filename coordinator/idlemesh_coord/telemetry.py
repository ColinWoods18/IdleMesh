"""Node telemetry: idle samples and node-state changes (S2.3 to S2.6).

Agents buffer records on disk and upload them in batches (``POST /api/v1/devices/{id}/telemetry``). Uploads
are retried after outages, so ingest is idempotent: a record is identified by its node and timestamp, and
a repeat is ignored. Records are stored under a pseudonymous node key (an HMAC of the device ID with a
per-site salt) when the site asks for it, so exported data cannot be joined to device names without the
salt, which never leaves the coordinator. Nothing here may carry prompt or response text (D-11).
"""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta, timezone
from pathlib import Path

from . import fsperm, obs
from .store import Store, iso, now

log = logging.getLogger("idlemesh_coord.telemetry")

SALT_FILE = "telemetry-salt"


class NodeKeys:
    """Maps device IDs to the keys telemetry is stored under."""

    def __init__(self, data_dir: str, pseudonymize: bool):
        self.pseudonymize = pseudonymize
        self._salt = _load_salt(Path(data_dir) / SALT_FILE) if pseudonymize else b""

    def key(self, device_id: str) -> str:
        if not self.pseudonymize:
            return device_id
        return "n_" + hmac.new(self._salt, device_id.encode(), hashlib.sha256).hexdigest()[:24]


def _load_salt(path: Path) -> bytes:
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    salt = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fsperm.write_private(path, salt.hex().encode())
    return salt


# Which consent each record kind needs (S2.6, S3.5): what the person at the machine does is idle data; how the
# node itself is doing is diagnostics.
IDLE_KINDS = frozenset({"idle", "state", "lid"})
DIAGNOSTIC_KINDS = frozenset({"health", "errors"})
COORDINATOR_NODE = "coordinator"  # the `node` of error counts the coordinator logged itself


def has_consent(consent: dict | None, kinds=frozenset({"idle"})) -> bool:
    """Whether the consent record covers every record kind in `kinds`."""
    consent = consent or {}
    if set(kinds) & IDLE_KINDS and not consent.get("idle_data"):
        return False
    if set(kinds) & DIAGNOSTIC_KINDS and not consent.get("diagnostics"):
        return False
    return True


def ingest(store: Store, node: str, batch, org_id: str | None, site_id: str | None) -> dict:
    """Store one validated batch. Returns how many records were new and how many were repeats."""
    received = iso(now())
    new = 0
    with store.tx() as c:
        for r in batch.records:
            ts = _ts(r.ts)
            if r.kind == "idle":
                cur = c.execute(
                    "INSERT OR IGNORE INTO idle_samples (node, ts, org_id, site_id, seconds_since_input, session_locked,"
                    " cpu_util, gpu_util, mem_free_gb, on_battery, battery_pct, state, received_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (node, ts, org_id, site_id, r.seconds_since_input, _bool(r.session_locked), r.cpu_util,
                     r.gpu_util, r.mem_free_gb, int(r.on_battery), r.battery_pct, r.state, received))
            elif r.kind == "state":
                cur = c.execute(
                    "INSERT OR IGNORE INTO node_events (node, ts, kind, from_state, to_state, org_id, site_id,"
                    " received_at) VALUES (?,?,?,?,?,?,?,?)",
                    (node, ts, "state", r.from_state, r.to, org_id, site_id, received))
            elif r.kind == "lid":
                cur = c.execute(
                    "INSERT OR IGNORE INTO node_events (node, ts, kind, from_state, to_state, org_id, site_id,"
                    " received_at) VALUES (?,?,?,?,?,?,?,?)",
                    (node, ts, "lid", None, "closed" if r.closed else "open", org_id, site_id, received))
            elif r.kind == "health":
                cur = c.execute(
                    "INSERT OR IGNORE INTO node_health (node, ts, org_id, site_id, cpu_util, gpu_util, mem_used_gb,"
                    " gpu_mem_used_gb, temp_c, cache_disk_free_gb, agent_uptime_s, worker_restarts, cap_kills,"
                    " tunnel_errors, telemetry_buffer_bytes, telemetry_dropped, received_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (node, ts, org_id, site_id, r.cpu_util, r.gpu_util, r.mem_used_gb, r.gpu_mem_used_gb, r.temp_c,
                     r.cache_disk_free_gb, r.agent_uptime_s, r.worker_restarts, r.cap_kills, r.tunnel_errors,
                     r.telemetry_buffer_bytes, r.telemetry_dropped, received))
            else:  # errors: one row per code, all or none (a retried batch repeats every code)
                inserted = 0
                for code, count in r.counts.items():
                    inserted += c.execute(
                        "INSERT OR IGNORE INTO error_events (node, ts, source, code, count, org_id, site_id)"
                        " VALUES (?,?,?,?,?,?,?)", (node, ts, "agent", code, count, org_id, site_id)).rowcount
                new += int(inserted > 0)
                continue
            new += cur.rowcount
        if batch.dropped_since_last:
            c.execute("INSERT INTO telemetry_drops (node, at, count) VALUES (?,?,?)",
                      (node, received, batch.dropped_since_last))
    if batch.dropped_since_last:
        obs.log(log, logging.WARNING, "node reported dropped telemetry", error_code="TELEMETRY_BUFFER_FULL",
                dropped=batch.dropped_since_last)
    return {"accepted": new, "duplicates": len(batch.records) - new}


def prune(store: Store, retention_days: int) -> int:
    """Delete telemetry older than the site's retention. Returns the number of rows removed."""
    cutoff = iso(now() - timedelta(days=retention_days))
    removed = 0
    with store.tx() as c:
        for table, col in (("idle_samples", "ts"), ("node_events", "ts"), ("telemetry_drops", "at"),
                           ("node_health", "ts"), ("error_events", "ts")):
            removed += c.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
    if removed:
        obs.log(log, logging.INFO, "telemetry retention applied", removed=removed, retention_days=retention_days)
    return removed


def timeline(store: Store, node: str, since) -> dict:
    """Samples, state changes and drops for one node since a time, oldest first."""
    t = iso(since)
    samples = [dict(r) for r in store.all(
        "SELECT ts, seconds_since_input, session_locked, cpu_util, gpu_util, mem_free_gb, on_battery, battery_pct, state"
        " FROM idle_samples WHERE node = ? AND ts >= ? ORDER BY ts", (node, t))]
    for s in samples:
        s["session_locked"] = None if s["session_locked"] is None else bool(s["session_locked"])
        s["on_battery"] = bool(s["on_battery"])
    events = [dict(r) for r in store.all(
        "SELECT ts, from_state, to_state FROM node_events WHERE node = ? AND kind = 'state' AND ts >= ? ORDER BY ts",
        (node, t))]
    dropped = store.one("SELECT COALESCE(SUM(count), 0) AS n FROM telemetry_drops WHERE node = ? AND at >= ?",
                        (node, t))["n"]
    return {"samples": samples, "events": events, "dropped": dropped}


def record_coordinator_errors(store: Store, counts: dict[str, int], org_id: str | None, site_id: str | None) -> None:
    """Error codes the coordinator itself logged since the last call (obs.take_error_counts), one row each."""
    if not counts:
        return
    ts = _ts(now())
    with store.tx() as c:
        for code, n in counts.items():
            c.execute("INSERT OR REPLACE INTO error_events (node, ts, source, code, count, org_id, site_id)"
                      " VALUES (?,?,?,?,?,?,?)", (COORDINATOR_NODE, ts, "coordinator", code, n, org_id, site_id))


def _ts(dt) -> str:
    # Millisecond precision, UTC: the same instant always gives the same key, so retries deduplicate.
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _bool(v: bool | None) -> int | None:
    return None if v is None else int(v)
