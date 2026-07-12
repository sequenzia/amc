"""Structured JSON logging with secret redaction.

Configures `structlog` so every log line is one JSON object with the standard
shape ``{ts, level, event, component?, ...kwargs}``. Sensitive fields are
recursively scrubbed before serialization.

Log files land in ``$AMG_LOG_DIR`` (default ``~/Library/Logs/messaging-agent``)
as ``adapter-YYYY-MM-DD.log``, rotated daily at local midnight, keeping the
last 14 files. If the directory cannot be created or written to, a clear
error is emitted on stderr and the logger falls back to stdout-only.

Wire FastAPI/Uvicorn into structlog with :func:`install_request_logging`.

Source: specs/agent-messaging-gateway-SPEC.md §6.2 (Data Protection /
secret redaction) and §11.3 (logging destination).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import time
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

import structlog

__all__ = [
    "DEFAULT_LOG_DIR",
    "LOG_FILE_PREFIX",
    "REDACTED",
    "RedactingProcessor",
    "configure_logging",
    "install_request_logging",
    "resolve_log_dir",
]

DEFAULT_LOG_DIR = "~/Library/Logs/messaging-agent"
LOG_FILE_PREFIX = "adapter"
LOG_FILE_SUFFIX = ".log"
ROTATION_BACKUP_COUNT = 14
REDACTED = "[REDACTED]"

# Field names matching these patterns get their VALUES replaced with [REDACTED].
# Match is case-insensitive against the key name.
_SECRET_EXACT = frozenset(
    {
        "authorization",
        "x-amg-signature",
        "password",
        "bearer",
    }
)
_SECRET_SUFFIX_PATTERN = re.compile(r".*(_token|_secret)$", re.IGNORECASE)


def _is_secret_key(key: Any) -> bool:
    """Return True if the field name `key` should have its value redacted."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in _SECRET_EXACT:
        return True
    return bool(_SECRET_SUFFIX_PATTERN.match(lowered))


def _redact_value(value: Any) -> Any:
    """Recursively walk `value`, redacting any nested dict entries with secret keys.

    - Mapping → new dict, secret keys get REDACTED, others recursed.
    - Non-string Iterable → list with each item recursed (catches ``list[dict]``).
    - Anything else → returned as-is.
    """
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k):
                out[k] = REDACTED
            else:
                out[k] = _redact_value(v)
        return out
    if isinstance(value, str | bytes | bytearray):
        return value
    if isinstance(value, Iterable):
        return [_redact_value(item) for item in value]
    return value


class RedactingProcessor:
    """structlog processor that scrubs secrets from the event dict in place.

    Redacts:
      - top-level keys matching the secret patterns
      - any nested dict (recursively) under non-secret keys
      - lists of dicts (e.g. headers serialized as a list of `{name, value}`)
    """

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        for key in list(event_dict.keys()):
            if _is_secret_key(key):
                event_dict[key] = REDACTED
            else:
                event_dict[key] = _redact_value(event_dict[key])
        return event_dict


def resolve_log_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the configured log directory, expanded to an absolute path.

    Honors ``AMG_LOG_DIR`` from `env` (falling back to ``os.environ``), then
    the spec default ``~/Library/Logs/messaging-agent``. The path is
    user-expanded but NOT created — the caller decides creation semantics.
    """
    source = env if env is not None else os.environ
    raw = source.get("AMG_LOG_DIR") or DEFAULT_LOG_DIR
    return Path(raw).expanduser()


def _build_file_handler(log_dir: Path) -> logging.handlers.TimedRotatingFileHandler:
    """Construct the daily-rotating file handler.

    Filename pattern: ``adapter-YYYY-MM-DD.log``. Rotation happens at local
    midnight; the previous day's file is renamed with the ``.YYYY-MM-DD``
    suffix that ``TimedRotatingFileHandler`` appends. Up to 14 historic
    files are retained.
    """
    today = time.strftime("%Y-%m-%d")
    filename = log_dir / f"{LOG_FILE_PREFIX}-{today}{LOG_FILE_SUFFIX}"
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(filename),
        when="midnight",
        interval=1,
        backupCount=ROTATION_BACKUP_COUNT,
        encoding="utf-8",
        utc=False,
    )
    # Emit raw JSON; structlog already produced the formatted message.
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _build_stream_handler(stream: Any = None) -> logging.StreamHandler:
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _shared_processors() -> list[Any]:
    """Processor chain that yields the standard log shape.

    Order matters:
      1. add log level → ``level``
      2. timestamp → ``ts`` (ISO8601, UTC, with 'Z')
      3. format exception info → ``exception`` (string traceback)
      4. RedactingProcessor → scrub secrets last so all kwargs (including
         headers added by callers) get walked.
      5. JSON renderer → final string.
    """
    return [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.format_exc_info,
        RedactingProcessor(),
        structlog.processors.JSONRenderer(sort_keys=True),
    ]


def configure_logging(
    *,
    log_dir: Path | str | None = None,
    level: int | str = logging.INFO,
    stderr: Any = None,
    stdout: Any = None,
) -> dict[str, Any]:
    """Initialize structlog + stdlib logging for the adapter.

    Parameters
    ----------
    log_dir
        Directory to write rotating log files. ``None`` resolves from
        ``AMG_LOG_DIR`` then the spec default.
    level
        Minimum log level (passed to the root logger).
    stderr, stdout
        Streams used for the fallback error message and stdout handler.
        Tests inject ``io.StringIO`` here.

    Returns
    -------
    dict
        Diagnostic info: ``{"log_dir": Path, "fallback": bool, "handlers": [...]}``.
        ``fallback`` is True iff the file handler could not be created and we
        fell back to stdout-only.
    """
    err = stderr if stderr is not None else sys.stderr
    out = stdout if stdout is not None else sys.stdout

    resolved_dir = Path(log_dir).expanduser() if log_dir is not None else resolve_log_dir()

    handlers: list[logging.Handler] = []
    fallback = False
    file_handler: logging.handlers.TimedRotatingFileHandler | None = None
    try:
        resolved_dir.mkdir(parents=True, exist_ok=True)
        file_handler = _build_file_handler(resolved_dir)
        handlers.append(file_handler)
    except OSError as exc:
        fallback = True
        print(
            f"[amg.logging] log directory unwritable ({resolved_dir}): {exc}; "
            "falling back to stdout-only",
            file=err,
        )

    if fallback:
        handlers.append(_build_stream_handler(out))

    # Reset root logger so repeated configure_logging() calls (tests) don't
    # accumulate handlers.
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)

    structlog.configure(
        processors=_shared_processors(),
        wrapper_class=structlog.make_filtering_bound_logger(
            level if isinstance(level, int) else logging.getLevelName(level)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    return {
        "log_dir": resolved_dir,
        "fallback": fallback,
        "handlers": handlers,
        "file_handler": file_handler,
    }


# ---------------------------------------------------------------------------
# FastAPI / Uvicorn integration
# ---------------------------------------------------------------------------


def install_request_logging(app: Any) -> None:
    """Install request-logging middleware and silence Uvicorn's access log.

    Adds an HTTP middleware that emits one structured log line per request
    with ``event=request method=... path=... status=... duration_ms=...``.
    Also mutes the duplicate uvicorn access logger so we don't get two lines
    per request.
    """
    log = structlog.get_logger("amg.http")

    @app.middleware("http")
    async def _log_requests(request: Any, call_next: Any) -> Any:
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = getattr(response, "status_code", 200)
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 3)
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=duration_ms,
            )

    # Mute uvicorn's own access logger; our middleware covers it.
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False
