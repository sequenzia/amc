"""Structured JSON logging for the receiver.

Mirrors the adapter's pattern (``amc/core/logging.py``) so logs from the
receiver and the adapter live side-by-side in the same directory and share
the same redaction rules. The output filename uses the ``receiver`` prefix
to keep the two streams distinguishable when tailing.

Note: the redaction logic is duplicated here (rather than imported from the
``amc`` package) to keep the receiver workspace decoupled. If the rules
diverge in the adapter, mirror them here.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import time
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

import structlog

LOG_FILE_PREFIX = "receiver"
LOG_FILE_SUFFIX = ".log"
ROTATION_BACKUP_COUNT = 14
REDACTED = "[REDACTED]"

_SECRET_EXACT = frozenset(
    {
        "authorization",
        "x-amc-signature",
        "password",
        "bearer",
    }
)
_SECRET_SUFFIX_PATTERN = re.compile(r".*(_token|_secret)$", re.IGNORECASE)


__all__ = [
    "LOG_FILE_PREFIX",
    "REDACTED",
    "RedactingProcessor",
    "configure_logging",
]


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in _SECRET_EXACT:
        return True
    return bool(_SECRET_SUFFIX_PATTERN.match(lowered))


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            out[k] = REDACTED if _is_secret_key(k) else _redact_value(v)
        return out
    if isinstance(value, str | bytes | bytearray):
        return value
    if isinstance(value, Iterable):
        return [_redact_value(item) for item in value]
    return value


class RedactingProcessor:
    """structlog processor that scrubs secrets from the event dict in place."""

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


def _build_file_handler(log_dir: Path) -> logging.handlers.TimedRotatingFileHandler:
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
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _build_stream_handler(stream: Any = None) -> logging.StreamHandler:
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _shared_processors() -> list[Any]:
    return [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.format_exc_info,
        RedactingProcessor(),
        structlog.processors.JSONRenderer(sort_keys=True),
    ]


def configure_logging(
    *,
    log_dir: Path,
    level: int | str = logging.INFO,
    stderr: Any = None,
    stdout: Any = None,
) -> dict[str, Any]:
    """Initialize root + structlog logging."""

    err = stderr if stderr is not None else sys.stderr
    out = stdout if stdout is not None else sys.stdout

    handlers: list[logging.Handler] = []
    fallback = False
    file_handler: logging.handlers.TimedRotatingFileHandler | None = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = _build_file_handler(log_dir)
        handlers.append(file_handler)
    except OSError as exc:
        fallback = True
        print(
            f"[amc.receiver.logging] log directory unwritable ({log_dir}): {exc}; "
            "falling back to stdout-only",
            file=err,
        )

    if fallback:
        handlers.append(_build_stream_handler(out))

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
        "log_dir": log_dir,
        "fallback": fallback,
        "handlers": handlers,
        "file_handler": file_handler,
    }
