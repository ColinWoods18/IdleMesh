"""Structured JSON logging for the coordinator (D-11, sprint 1).

Every log line is one JSON object that carries the IDs needed to follow work across components:

- ``trace_id``: one HTTP exchange. Agents send ``X-Trace-Id`` with each call and log the same value, so
  an agent's line and the coordinator's line for one heartbeat can be matched.
- ``request_id``: one inference request (``req_...``), the same ID the API returns.
- ``node_id``: the enrolled device (``dev_...``).
- ``deployment_id``: the model deployment (``dep_...``).

IDs are held in context variables, so they follow ``await`` and asyncio tasks automatically.

Redaction (S1.9): every finished line passes through ``redact()`` before it is written. It removes
private keys, bearer tokens, API keys, secret-looking fields, prompt and response text fields, and
user names inside home-directory paths. Prompt and response text is also never passed to the logger
by design; redaction is the safety net, and tests enforce both.
"""
import contextvars
import json
import logging
import logging.handlers
import re
import sys
import threading
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

COMPONENT = "coordinator"

trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
node_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("node_id", default=None)
deployment_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("deployment_id", default=None)
_IDS = {"trace_id": trace_id, "request_id": request_id, "node_id": node_id, "deployment_id": deployment_id}

# Fixed for the process: which organisation and site this coordinator serves (from the site config).
_SITE: dict[str, str] = {}


def set_site(org_id: str, site_id: str) -> None:
    _SITE.update(org_id=org_id, site_id=site_id)


_TRACE_ID_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def accept_trace_id(value: str | None) -> str:
    """Use the caller's trace ID if it is well formed; otherwise start a new one."""
    return value if value and _TRACE_ID_OK.match(value) else new_trace_id()


@contextmanager
def bound(**ids):
    """Set correlation IDs for the duration of a block (and anything it awaits or starts)."""
    tokens = [(_IDS[k], _IDS[k].set(v)) for k, v in ids.items() if v is not None]
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


# --- redaction ---------------------------------------------------------------------------------

# Field names whose values are never logged. Matched case-insensitively against JSON keys and
# key=value pairs. Content fields cover prompts, replies and chat history.
SECRET_KEYS = ("password", "passphrase", "secret", "token", "device_token", "api_key", "apikey", "authorization",
               "cookie", "csrf", "recovery", "recovery_code", "private_key", "key_pem", "tunnel_key_pem", "code")
CONTENT_KEYS = ("prompt", "content", "messages", "message_text", "text", "response", "completion", "reply",
                "history", "title")
REDACTED = "[REDACTED]"

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_API_KEY = re.compile(r"\bdifa_[A-Za-z0-9_-]{8,}")
_KEYS_ALT = "|".join(sorted({*SECRET_KEYS, *CONTENT_KEYS}, key=len, reverse=True))
# "key": "value"   (JSON, value may contain escaped quotes)
_JSON_PAIR = re.compile(r'("(?:%s)"\s*:\s*)"(?:[^"\\]|\\.)*"' % _KEYS_ALT, re.I)
# key=value or key: value in free text, up to whitespace, comma or closing bracket
_TEXT_PAIR = re.compile(r"(?<![A-Za-z0-9_])((?:%s)\s*[=:]\s*)(?!\[REDACTED\])[^\s,;)\]}\"']+" % _KEYS_ALT, re.I)
# The user name inside home-directory paths (Windows, macOS, Linux), with either slash style.
_HOME = re.compile(r"(?i)((?:[A-Z]:)?(?:\\\\|\\|/)(?:Users|home)(?:\\\\|\\|/))[^\\/\s\"']+")


def redact(text: str) -> str:
    text = _PEM.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _BEARER.sub(r"\1 " + REDACTED, text)
    text = _API_KEY.sub("difa_" + REDACTED, text)
    text = _JSON_PAIR.sub(r'\1"' + REDACTED + '"', text)
    text = _TEXT_PAIR.sub(r"\1" + REDACTED, text)
    text = _HOME.sub(r"\1<user>", text)
    return text


def _clean_fields(fields: dict) -> dict:
    """Drop secret and content keys from structured fields before they are serialised."""
    out = {}
    for k, v in fields.items():
        lk = k.lower()
        if lk in SECRET_KEYS or lk in CONTENT_KEYS:
            out[k] = REDACTED
        elif isinstance(v, dict):
            out[k] = _clean_fields(v)
        else:
            out[k] = v
    return out


# --- formatting --------------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """One redacted JSON object per line, in the LogEvent shape from PROJECT_PLAN §6."""

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "component": COMPONENT,
            "logger": record.name,
            "message": record.getMessage(),
            "version": __version__,
            **_SITE,
        }
        for k, var in _IDS.items():
            v = getattr(record, k, None) or var.get()
            if v:
                line[k] = v
        code = getattr(record, "error_code", None)
        if code:
            line["error_code"] = code
        fields = getattr(record, "fields", None)
        if fields:
            line["fields"] = _clean_fields(fields)
        if record.exc_info:
            line["exception"] = "".join(traceback.format_exception(*record.exc_info))[-8000:]
        return redact(json.dumps(line, default=str, ensure_ascii=False))


class ErrorCounter(logging.Handler):
    """Counts log events that carry an error code (S3.6 error-rate panel). Codes only, never messages."""

    def __init__(self):
        super().__init__(logging.INFO)
        self._counts: dict[str, int] = {}
        self._lock_counts = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        code = getattr(record, "error_code", None)
        if code:
            with self._lock_counts:
                self._counts[code] = self._counts.get(code, 0) + 1

    def take(self) -> dict[str, int]:
        with self._lock_counts:
            out, self._counts = self._counts, {}
        return out


_COUNTER = ErrorCounter()


def install_error_counter() -> None:
    """Attach the counter to the root logger (idempotent; setup_logging replaces handlers, so call it after)."""
    root = logging.getLogger()
    if _COUNTER not in root.handlers:
        root.addHandler(_COUNTER)


def take_error_counts() -> dict[str, int]:
    """Error codes logged since the last call, for the coordinator's own row in error_events."""
    return _COUNTER.take()


def log(logger: logging.Logger, level: int, message: str, *, error_code: str | None = None, **fields) -> None:
    """Log one event with an optional catalogued error code and structured fields."""
    extra = {"fields": fields} if fields else {}
    if error_code:
        extra["error_code"] = error_code
    logger.log(level, message, extra=extra)


def setup_logging(level: str = "info", log_file: str | None = None, max_bytes: int = 10 * 1024 * 1024,
                  backups: int = 5, stream=None) -> None:
    """Send all coordinator, uvicorn and library logs through the JSON formatter.

    Logs go to stderr, and also to a size-capped rotating file when ``log_file`` is set
    (``max_bytes`` per file, ``backups`` old files kept).
    """
    formatter = JsonFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backups,
                                                             encoding="utf-8"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # The access middleware logs each exchange with its trace ID, so uvicorn's own access log is redundant.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
