"""Site configuration: one YAML file per coordinator (S1.12).

``config/example.site.yaml`` documents every key. Validation is strict (unknown keys are errors) so a typo
in an admin's file fails at startup instead of being silently ignored. Sections for later sprints are
validated and kept now so the file format is stable; ``Settings.from_site`` applies what exists today
(queue limits and deadlines), and ``obs.setup_logging`` takes the logging section.
"""
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ENV = "IDLEMESH_SITE_CONFIG"
_ID = r"^[a-z0-9][a-z0-9-]{0,62}$"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Site(_Strict):
    org_id: str = Field(pattern=_ID)
    site_id: str = Field(pattern=_ID)
    name: str = Field(min_length=1, max_length=200)


class Logging(_Strict):
    level: Literal["error", "warn", "warning", "info", "debug"] = "info"
    file: str | None = None
    max_mb: float = Field(10, gt=0, le=1024)
    keep: int = Field(5, ge=0, le=100)


class Telemetry(_Strict):
    tier: Literal["minimal", "operational", "research"] = "operational"
    interval_s: int = Field(60, ge=10, le=3600)
    retention_days: int = Field(90, ge=1, le=3650)
    pseudonymize_nodes: bool = True


class Idle(_Strict):
    idle_after_s: int = Field(600, ge=30)
    ramp_s: int = Field(1800, ge=0)
    yield_level: float = Field(0.02, ge=0, le=1)
    yield_within_ms: int = Field(1000, ge=100, le=60_000)


class Window(_Strict):
    name: str = Field(min_length=1, max_length=60)
    days: list[str]
    start: str
    end: str
    max_level: float = Field(ge=0, le=1)

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if d not in _DAYS]
        if not v or bad:
            raise ValueError(f"days must be a non-empty list of {sorted(_DAYS)}; got {bad or v}")
        return v

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"times are 24-hour HH:MM; got {v!r}")
        return v


class Contribution(_Strict):
    max_memory_fraction: float = Field(0.5, gt=0, le=1)
    max_cpu_fraction: float = Field(0.75, gt=0, le=1)
    allow_battery: bool = False
    windows: list[Window] = []


class Probes(_Strict):
    rtt_every_s: int = Field(300, ge=10)
    bandwidth_every_h: float = Field(24, gt=0)
    bandwidth_off_hours_only: bool = True


class Fleet(_Strict):
    min_active_nodes: int = Field(1, ge=0)
    max_active_nodes: int = Field(50, ge=1)
    warm_standbys: int = Field(1, ge=0)

    @model_validator(mode="after")
    def _bounds(self):
        if self.min_active_nodes > self.max_active_nodes:
            raise ValueError("min_active_nodes cannot exceed max_active_nodes")
        return self


class Queue(_Strict):
    per_user: int = Field(2, ge=1, le=100)
    total: int = Field(10, ge=1, le=100_000)
    active_generations: int = Field(1, ge=1, le=10_000)
    wait_deadline_s: float = Field(600, gt=0)
    active_deadline_s: float = Field(300, gt=0)


class SiteConfig(_Strict):
    site: Site
    logging: Logging = Logging()
    telemetry: Telemetry = Telemetry()
    idle: Idle = Idle()
    contribution: Contribution = Contribution()
    probes: Probes = Probes()
    fleet: Fleet = Fleet()
    queue: Queue = Queue()
    # Directory of the file it was loaded from; relative paths (the log file) resolve against it.
    base_dir: str = "."

    def log_file(self) -> str | None:
        if not self.logging.file:
            return None
        p = Path(self.logging.file)
        return str(p if p.is_absolute() else Path(self.base_dir) / p)


class SiteConfigError(ValueError):
    pass


def load(path: str | os.PathLike) -> SiteConfig:
    """Read and validate a site file; raise SiteConfigError with every problem listed."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise SiteConfigError(f"cannot read site config {path}: {e}") from None
    if not isinstance(data, dict):
        raise SiteConfigError(f"site config {path} must be a mapping of sections")
    try:
        return SiteConfig(**data, base_dir=str(path.resolve().parent))
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        raise SiteConfigError(f"site config {path} is invalid: {problems}") from None


def from_env() -> SiteConfig | None:
    path = os.environ.get(ENV)
    return load(path) if path else None
