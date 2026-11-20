"""Request and response schemas. Unknown fields are rejected (spec 9.2)."""
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ..domain.access import Visibility


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Passphrase = Field(min_length=15, max_length=1024)
Login = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class CreateGroupIn(Strict):
    name: str = Field(min_length=1, max_length=100)
    owner_login: str = Login
    owner_display_name: str = Field(min_length=1, max_length=100)
    passphrase: str = Passphrase


class LoginIn(Strict):
    group_id: str
    login: str = Login
    passphrase: str = Field(min_length=1, max_length=1024)


class RecoverIn(Strict):
    group_id: str
    login: str = Login
    recovery_code: str = Field(min_length=1, max_length=200)
    new_passphrase: str = Passphrase


class InviteIn(Strict):
    role: Literal["member", "admin"] = "member"
    ttl_hours: int = Field(default=24, ge=1, le=168)


class CodeIn(Strict):
    code: str = Field(min_length=1, max_length=200)


class RedeemIn(Strict):
    code: str = Field(min_length=1, max_length=200)
    login: str = Login
    display_name: str = Field(min_length=1, max_length=100)
    passphrase: str = Passphrase


class CredentialIn(Strict):
    name: str = Field(min_length=1, max_length=100)
    # "metrics" makes a read-only key for dashboards (S3.6): administrators only, and it carries no other scope.
    scopes: list[Literal["inference", "share", "metrics"]] = ["inference"]
    expires_days: int = Field(default=30, ge=1, le=90)


class DecodeProbe(Strict):
    model: str = Field(max_length=100)
    tokens_per_s: float = Field(gt=0)
    backend: str = Field(max_length=40)
    threads: int | None = Field(default=None, ge=1)


class WorkerStatus(Strict):
    state: Literal["running", "stopped"]
    endpoint: str = Field(max_length=100)
    rss_bytes: int = Field(ge=0)
    peak_rss_bytes: int = Field(ge=0)
    cap_kills: int = Field(ge=0)
    last_stop_reason: str | None = Field(default=None, max_length=200)
    transport: Literal["plaintext", "mtls"] = "plaintext"


class PrefetchStatus(Strict):
    deployment_id: str = Field(max_length=64)
    state: Literal["fetching", "ready", "failed"]
    bytes_done: int = Field(ge=0)
    bytes_total: int = Field(ge=0)
    error: str | None = Field(default=None, max_length=300)


class Gpu(Strict):
    vendor: Literal["nvidia", "apple", "amd", "intel", "other"]
    name: str = Field(max_length=120)
    vram_bytes: int | None = Field(default=None, ge=0)  # None for unified memory or when the OS cannot tell
    driver: str | None = Field(default=None, max_length=60)


Backend = Literal["cuda", "metal", "vulkan", "rocm", "cpu"]


class Hardware(Strict):
    """The node's hardware profile (S3.3) and the inference backends it can run (S3.4)."""
    cpu_model: str = Field(max_length=120)
    cpu_cores: int | None = Field(default=None, ge=1)
    cpu_threads: int = Field(ge=1)
    ram_bytes: int = Field(ge=0)
    unified_memory: bool = False
    gpus: list[Gpu] = Field(default=[], max_length=16)
    backends: list[Backend] = Field(default=["cpu"], max_length=8)
    idle_source: Literal["win32", "iokit", "logind", "tty", "none"] = "none"


class Capabilities(Strict):
    os: str = Field(max_length=40)
    arch: str = Field(max_length=40)
    backend_digest: str = Field(max_length=100)
    formats: list[str] = Field(max_length=10)
    allocatable_bytes: int = Field(ge=0)
    mem_bandwidth_gbps: float = Field(gt=0)
    rtt_ms: float = Field(ge=0)
    power: Literal["ac", "battery", "unknown"] = "unknown"
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    decode_probe: DecodeProbe | None = None
    hardware: Hardware | None = None  # absent from simulated nodes and agents older than S3.3


class Policy(Strict):
    memory_cap_bytes: int = Field(ge=0)
    allow_battery: bool = False
    battery_stop_pct: float = Field(default=30, ge=0, le=100)
    battery_resume_pct: float = Field(default=40, ge=0, le=100)
    paused: bool = False
    warm_hold_minutes: int = Field(default=30, ge=0, le=1440)


NodeState = Literal["idle", "active", "sleeping", "draining", "serving"]


class Consent(Strict):
    """The owner's consent record, kept on the device and reported with every heartbeat (S2.6)."""
    idle_data: bool = False
    diagnostics: bool = False
    policy_version: str = Field(default="v0", max_length=20)
    recorded_at: str | None = Field(default=None, max_length=40)


class TelemetryStatus(Strict):
    buffer_bytes: int = Field(ge=0)
    dropped: int = Field(ge=0)


class EnrollIn(Strict):
    code: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=1, max_length=4000)  # PEM certificate for the worker tunnel
    capabilities: Capabilities
    policy: Policy
    consent: Consent | None = None


class HeartbeatIn(Strict):
    capabilities: Capabilities
    paused: bool = False  # effective local state after the agent applies owner policy
    worker: WorkerStatus | None = None  # present when the agent supervises an inference worker
    prefetch: PrefetchStatus | None = None  # progress on this device's partition download (FR 30)
    consent: Consent | None = None  # absent from agents older than S2.6: the stored record stays
    node_state: NodeState | None = None
    telemetry: TelemetryStatus | None = None


class IdleRecord(Strict):
    """One idle sample (PROJECT_PLAN §6 IdleSample). Never anything typed or shown on the machine."""
    kind: Literal["idle"]
    ts: AwareDatetime
    seconds_since_input: int | None = Field(ge=0)
    session_locked: bool | None
    cpu_util: float = Field(ge=0, le=1)
    gpu_util: float | None = Field(default=None, ge=0, le=1)  # S3.5; absent without an NVIDIA GPU
    mem_free_gb: float = Field(ge=0)
    on_battery: bool
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    state: NodeState


class StateRecord(Strict):
    """A node-state change. After a sleep the agent sends one at the start of the gap and one on waking."""
    kind: Literal["state"]
    ts: AwareDatetime
    from_state: NodeState | None = Field(alias="from")
    to: NodeState


class LidRecord(Strict):
    """A laptop lid opened or closed (S3.1). Sent once at start and on every change."""
    kind: Literal["lid"]
    ts: AwareDatetime
    closed: bool


class HealthRecord(Strict):
    """Node health (S3.5; PROJECT_PLAN §6 NodeHealthSample). Counters count from the agent's start and reset
    only together with agent_uptime_s, so a reset is never silent."""
    kind: Literal["health"]
    ts: AwareDatetime
    cpu_util: float = Field(ge=0, le=1)
    gpu_util: float | None = Field(default=None, ge=0, le=1)
    mem_used_gb: float = Field(ge=0)
    gpu_mem_used_gb: float | None = Field(default=None, ge=0)
    temp_c: float | None = Field(default=None, ge=-50, le=150)
    cache_disk_free_gb: float | None = Field(default=None, ge=0)
    agent_uptime_s: int = Field(ge=0)
    worker_restarts: int = Field(ge=0)
    cap_kills: int = Field(ge=0)
    tunnel_errors: int = Field(ge=0)
    telemetry_buffer_bytes: int = Field(ge=0)
    telemetry_dropped: int = Field(ge=0)


ErrorCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]


class ErrorsRecord(Strict):
    """How many warnings and errors the agent logged per error code since its last record (S3.5). Codes only:
    the messages stay in the node's log file (and a diagnostic bundle, S4.4)."""
    kind: Literal["errors"]
    ts: AwareDatetime
    counts: dict[ErrorCode, Annotated[int, Field(ge=1)]] = Field(max_length=200)


TelemetryRecord = Annotated[IdleRecord | StateRecord | LidRecord | HealthRecord | ErrorsRecord,
                            Field(discriminator="kind")]


class TelemetryBatchIn(Strict):
    schema_version: Literal[1]
    records: list[TelemetryRecord] = Field(max_length=5000)
    dropped_since_last: int = Field(default=0, ge=0)


class PolicyPatch(Strict):
    memory_cap_bytes: int | None = Field(default=None, ge=0)
    allow_battery: bool | None = None
    battery_stop_pct: float | None = Field(default=None, ge=0, le=100)
    battery_resume_pct: float | None = Field(default=None, ge=0, le=100)
    paused: bool | None = None
    warm_hold_minutes: int | None = Field(default=None, ge=0, le=1440)
    disabled: bool | None = None  # administrators may only disable


class DeploymentIn(Strict):
    model: str
    require_all: bool = False
    prefetch: bool = False  # devices download and verify their own weights before loading (FR 30)


class ConversationIn(Strict):
    visibility: Visibility
    title: str | None = Field(default=None, max_length=120)


class ForkIn(Strict):
    visibility: Visibility
    include_message_ids: list[str] = []
    include_all: bool = False
    preview_confirmed: bool = False
    title: str | None = Field(default=None, max_length=120)


class MessageIn(Strict):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=200_000)


class RequestIn(Strict):
    model: str
    messages: list[MessageIn] = Field(min_length=1)
    visibility: Visibility
    max_output_tokens: int = Field(ge=1, le=512)
    branch_id: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    seed: int | None = None
    client_request_id: str | None = Field(default=None, max_length=100)
