"""Proposed defaults from spec v0.3. Every value here is a baseline to be revised in the decision log."""
import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


# Seconds an idle control connection stays open. Must exceed the agents' heartbeat interval (5 s) so
# heartbeats reuse one TLS connection instead of handshaking each time.
CONTROL_KEEP_ALIVE_S = 30


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("IDLEMESH_DB", "idlemesh.sqlite3"))
    # Coordinator tunnel key and certificate; defaults to the database's directory.
    tunnel_dir: str | None = field(default_factory=lambda: os.environ.get("IDLEMESH_TUNNEL_DIR"))
    # Certificate the control API is served with over HTTPS (set by scripts/dev_server.py). Its fingerprint
    # is returned with every enrollment code so agents can pin it before sending the code.
    control_cert: str | None = field(default_factory=lambda: os.environ.get("IDLEMESH_CONTROL_CERT"))
    # Sessions (spec 6.5)
    session_idle_s: int = 30 * 60
    session_absolute_s: int = 12 * 3600
    min_passphrase_len: int = 15
    login_attempts_per_window: int = 10
    login_window_s: int = 15 * 60
    # Invitations and credentials (FR 02, FR 23)
    invite_ttl_hours_default: int = 24
    invite_ttl_hours_max: int = 168
    credential_days_default: int = 30
    credential_days_max: int = 90
    # Queue and deadlines (FR 24, Section 7)
    per_member_queue: int = 2
    group_queue: int = 10
    active_generations: int = 1
    wait_deadline_s: float = field(default_factory=lambda: _env_float("IDLEMESH_WAIT_DEADLINE_S", 600))
    active_deadline_s: float = field(default_factory=lambda: _env_float("IDLEMESH_ACTIVE_DEADLINE_S", 300))
    # Request limits (spec 9.2)
    max_body_bytes: int = 1 << 20
    max_context_tokens: int = 4096
    max_output_tokens: int = 512
    # Device liveness (spec 9.4)
    heartbeat_interval_s: float = 5
    heartbeat_unassign_s: float = 15
    # Partition prefetch (FR 30): devices fetch their ranges from the model's origin URL, or from this
    # coordinator ("coordinator"), which suits a LAN or a test.
    model_source: str = field(default_factory=lambda: os.environ.get("IDLEMESH_MODEL_SOURCE", "origin"))
    prefetch_timeout_s: float = field(default_factory=lambda: _env_float("IDLEMESH_PREFETCH_TIMEOUT_S", 6 * 3600))
    # Prototype-only knobs
    fake_token_delay_s: float = field(default_factory=lambda: _env_float("IDLEMESH_FAKE_TOKEN_DELAY", 0.02))
    argon2_fast: bool = field(default_factory=lambda: os.environ.get("IDLEMESH_ARGON2_FAST") == "1")
    cookie_secure: bool = field(default_factory=lambda: os.environ.get("IDLEMESH_COOKIE_SECURE", "0") == "1")
    # Site identity from the site config file (S1.12); None when the coordinator runs without one.
    org_id: str | None = None
    site_id: str | None = None
    # Telemetry and idle detection (S2; site config sections `telemetry` and `idle`).
    telemetry_interval_s: int = 60
    telemetry_retention_days: int = 90
    pseudonymize_nodes: bool = True
    idle_after_s: int = 600
    # Contribution governor (S4.9, D-12; site config sections `idle` and `contribution`), sent to agents.
    ramp_s: int = 1800
    yield_level: float = 0.02
    max_cpu_fraction: float = 0.75
    contribution_windows: list = field(default_factory=list)  # [{"name", "days", "start", "end", "max_level"}]
    # Network figures from heartbeats (S5.1; site config section `probes`).
    rtt_refresh_h: float = 24
    rtt_change_pct: float = 50
    # Network clusters (S5.4; site config section `clusters`).
    cluster_max_rtt_ms: float = 5.0
    cluster_min_mbps: float = 100.0
    cluster_max_age_h: float = 72
    cluster_every_s: int = 300

    @classmethod
    def from_site(cls, site, **overrides) -> "Settings":
        """Settings with the site file's values applied (see config/example.site.yaml)."""
        q, t = site.queue, site.telemetry
        return cls(**{"org_id": site.site.org_id, "site_id": site.site.site_id, "per_member_queue": q.per_user,
                      "group_queue": q.total, "active_generations": q.active_generations,
                      "wait_deadline_s": q.wait_deadline_s, "active_deadline_s": q.active_deadline_s,
                      "telemetry_interval_s": t.interval_s, "telemetry_retention_days": t.retention_days,
                      "pseudonymize_nodes": t.pseudonymize_nodes, "idle_after_s": site.idle.idle_after_s,
                      "ramp_s": site.idle.ramp_s, "yield_level": site.idle.yield_level,
                      "max_cpu_fraction": site.contribution.max_cpu_fraction,
                      "contribution_windows": [w.model_dump() for w in site.contribution.windows],
                      "cluster_max_rtt_ms": site.clusters.max_rtt_ms, "cluster_min_mbps": site.clusters.min_mbps,
                      "rtt_refresh_h": site.probes.rtt_refresh_h, "rtt_change_pct": site.probes.rtt_change_pct,
                      "cluster_max_age_h": site.clusters.max_age_h, "cluster_every_s": site.clusters.recompute_every_s,
                      **overrides})
