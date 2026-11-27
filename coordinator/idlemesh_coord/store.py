"""SQLite persistence on coordinator-local disk (spec 6.3, 9.1). Single writer in v1.

Foreign keys and group_id filters enforce group boundaries. Timestamps are UTC ISO 8601.
Prompt and response text is stored only in the messages table and never written to audit or logs.
Prototype gap: content is not yet encrypted at rest with the group data key (spec 8.2).
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY, display_name TEXT NOT NULL, fingerprint TEXT NOT NULL,
  policy_version INTEGER NOT NULL DEFAULT 1, retention_days INTEGER NOT NULL DEFAULT 30, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), login TEXT NOT NULL,
  display_name TEXT NOT NULL, pass_hash TEXT NOT NULL, recovery_verifier TEXT NOT NULL,
  role TEXT NOT NULL, status TEXT NOT NULL, joined_at TEXT NOT NULL, revoked_at TEXT,
  UNIQUE (group_id, login));
CREATE TABLE IF NOT EXISTS invitations (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), verifier TEXT NOT NULL, role TEXT NOT NULL,
  issuer_id TEXT NOT NULL, expires_at TEXT NOT NULL, redeemed_at TEXT, redeemed_by TEXT, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  verifier TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES members(id), csrf TEXT NOT NULL,
  created_at TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES members(id), group_id TEXT NOT NULL,
  name TEXT NOT NULL, verifier TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL, expires_at TEXT NOT NULL,
  revoked_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), owner_id TEXT NOT NULL REFERENCES members(id),
  name TEXT NOT NULL, public_key TEXT NOT NULL, token_verifier TEXT NOT NULL, capabilities TEXT NOT NULL,
  capabilities_rev INTEGER NOT NULL DEFAULT 1, policy TEXT NOT NULL, policy_rev INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL, enrolled_at TEXT NOT NULL, last_seen TEXT, consent TEXT, log_override TEXT);
CREATE TABLE IF NOT EXISTS enrollment_codes (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), member_id TEXT NOT NULL REFERENCES members(id),
  verifier TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS deployments (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), model TEXT NOT NULL,
  backend_digest TEXT NOT NULL, placement TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS branches (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), owner_id TEXT NOT NULL REFERENCES members(id),
  visibility TEXT NOT NULL, title TEXT NOT NULL, source_branch_id TEXT, created_at TEXT NOT NULL,
  retention_deadline TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES branches(id), seq INTEGER NOT NULL, author_id TEXT,
  role TEXT NOT NULL, content TEXT NOT NULL, completion_state TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, attempt INTEGER NOT NULL DEFAULT 1, group_id TEXT NOT NULL, member_id TEXT NOT NULL,
  branch_id TEXT NOT NULL, deployment_id TEXT NOT NULL, visibility TEXT NOT NULL, status TEXT NOT NULL,
  context_message_ids TEXT NOT NULL, max_output_tokens INTEGER NOT NULL, temperature REAL, seed INTEGER,
  client_request_id TEXT, payload_fingerprint TEXT, finish_reason TEXT, prompt_tokens INTEGER,
  completion_tokens INTEGER, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS requests_idem ON requests(member_id, client_request_id)
  WHERE client_request_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS idle_samples (
  node TEXT NOT NULL, ts TEXT NOT NULL, org_id TEXT, site_id TEXT, seconds_since_input INTEGER,
  session_locked INTEGER, cpu_util REAL NOT NULL, mem_free_gb REAL NOT NULL, on_battery INTEGER NOT NULL,
  battery_pct REAL, state TEXT NOT NULL, received_at TEXT NOT NULL, gpu_util REAL, contribution_level REAL,
  level_reason TEXT, PRIMARY KEY (node, ts));
CREATE TABLE IF NOT EXISTS crash_reports (
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id), group_id TEXT NOT NULL, kind TEXT NOT NULL,
  ts TEXT NOT NULL, received_at TEXT NOT NULL, version TEXT, build_hash TEXT, os TEXT, summary TEXT NOT NULL,
  body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS crash_reports_device ON crash_reports(device_id, ts);
CREATE TABLE IF NOT EXISTS diagnostic_bundles (
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id), group_id TEXT NOT NULL,
  requested_by TEXT NOT NULL, requested_at TEXT NOT NULL, state TEXT NOT NULL, uploaded_at TEXT,
  size_bytes INTEGER, body TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS node_health (
  node TEXT NOT NULL, ts TEXT NOT NULL, org_id TEXT, site_id TEXT, cpu_util REAL NOT NULL, gpu_util REAL,
  mem_used_gb REAL NOT NULL, gpu_mem_used_gb REAL, temp_c REAL, cache_disk_free_gb REAL,
  agent_uptime_s INTEGER NOT NULL, worker_restarts INTEGER NOT NULL, cap_kills INTEGER NOT NULL,
  tunnel_errors INTEGER NOT NULL, telemetry_buffer_bytes INTEGER NOT NULL, telemetry_dropped INTEGER NOT NULL,
  received_at TEXT NOT NULL, PRIMARY KEY (node, ts));
CREATE INDEX IF NOT EXISTS node_health_ts ON node_health(ts);
CREATE TABLE IF NOT EXISTS error_events (
  node TEXT NOT NULL, ts TEXT NOT NULL, source TEXT NOT NULL, code TEXT NOT NULL, count INTEGER NOT NULL,
  org_id TEXT, site_id TEXT, PRIMARY KEY (node, ts, source, code));
CREATE INDEX IF NOT EXISTS error_events_ts ON error_events(ts);
CREATE TABLE IF NOT EXISTS node_events (
  node TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
  org_id TEXT, site_id TEXT, received_at TEXT NOT NULL, PRIMARY KEY (node, ts, kind, to_state));
CREATE TABLE IF NOT EXISTS net_edges (
  src TEXT NOT NULL, dst TEXT NOT NULL, ts TEXT NOT NULL, method TEXT NOT NULL, rtt_ms_p50 REAL, rtt_ms_p95 REAL,
  bandwidth_mbps REAL, samples INTEGER, org_id TEXT, site_id TEXT, received_at TEXT NOT NULL,
  PRIMARY KEY (src, dst, ts, method));
CREATE INDEX IF NOT EXISTS net_edges_ts ON net_edges(ts);
CREATE TABLE IF NOT EXISTS node_clusters (node TEXT PRIMARY KEY, cluster_id TEXT NOT NULL, since TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telemetry_drops (node TEXT NOT NULL, at TEXT NOT NULL, count INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idle_samples_ts ON idle_samples(ts);
CREATE INDEX IF NOT EXISTS node_events_ts ON node_events(ts);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT, actor_id TEXT, operation TEXT NOT NULL,
  object_id TEXT, result TEXT NOT NULL, at TEXT NOT NULL);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._db.executescript(SCHEMA)
        with self.tx() as c:
            if c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                c.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            self._migrate(c)

    @staticmethod
    def _migrate(c) -> None:
        """Bring a database made by an older coordinator up to SCHEMA_VERSION. New tables come from SCHEMA."""
        version = c.execute("SELECT version FROM schema_version").fetchone()[0]
        def add_column(table: str, column: str, decl: str) -> None:
            if column not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        if version < 2:  # S2.6: the owner's consent record on each device
            add_column("devices", "consent", "TEXT")
        if version < 3:  # S3.5: GPU use in idle samples (node_health and error_events come from SCHEMA)
            add_column("idle_samples", "gpu_util", "REAL")
        if version < 4:  # S4.5 remote log level; S4.9 contribution level (crash and bundle tables from SCHEMA)
            add_column("devices", "log_override", "TEXT")
            add_column("idle_samples", "contribution_level", "REAL")
            add_column("idle_samples", "level_reason", "TEXT")
        # version 5 (S5.4): net_edges and node_clusters are new tables, so they come from SCHEMA
        if version < SCHEMA_VERSION:
            c.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    @contextmanager
    def tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def one(self, sql: str, args=()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def all(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def audit(self, c, group_id, actor_id, operation, object_id=None, result="ok"):
        c.execute("INSERT INTO audit (group_id, actor_id, operation, object_id, result, at) VALUES (?,?,?,?,?,?)",
                  (group_id, actor_id, operation, object_id, result, iso(now())))

    # --- Branches and messages -------------------------------------------------------------
    def create_branch(self, c, branch_id, group_id, owner_id, visibility, title, retention_days, source=None):
        c.execute("INSERT INTO branches (id, group_id, owner_id, visibility, title, source_branch_id, created_at,"
                  " retention_deadline) VALUES (?,?,?,?,?,?,?,?)",
                  (branch_id, group_id, owner_id, visibility, title, source, iso(now()),
                   iso(now() + timedelta(days=retention_days))))

    def add_message(self, c, message_id, branch_id, author_id, role, content, state="complete"):
        seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE branch_id = ?", (branch_id,)).fetchone()[0]
        c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                  (message_id, branch_id, seq, author_id, role, content, state, iso(now())))

    def branch_messages(self, branch_id) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM messages WHERE branch_id = ? ORDER BY seq", (branch_id,))

    # --- Requests ----------------------------------------------------------------------------
    def set_request(self, request_id, **fields):
        fields["updated_at"] = iso(now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE requests SET {cols} WHERE id = ?", (*fields.values(), request_id))

    @staticmethod
    def dumps(obj) -> str:
        return json.dumps(obj, sort_keys=True)
