"""Receiver configuration loaded from the process environment.

Follows the env-driven pattern established in ``amg/core/auth.py`` and
``amg/core/webhook.py``: ``ENV_*`` constants + a frozen dataclass +
``from_env`` classmethod + a module-specific ``ReceiverConfigError``.

Required env vars
-----------------
* ``AMG_WEBHOOK_SECRET`` — shared HMAC-SHA256 secret with the adapter.
  This is the **same** env var the adapter uses, so both sides agree on
  one value.
* ``AMG_BEARER_TOKEN`` — passed through to the MCP wrapper subprocess.

Optional env vars (defaults shown)
----------------------------------
* ``AMG_RECEIVER_BIND_HOST``                ``127.0.0.1``
* ``AMG_RECEIVER_BIND_PORT``                ``8090``
* ``AMG_AGENT_ID``                          ``amg-receiver``
* ``AMG_RECEIVER_AGENT_PROMPT_FILE``        ``~/.config/messaging-agent/agent_prompt.md``
* ``AMG_RECEIVER_DANGEROUS``                ``0`` (set to ``1`` to pass
  ``--dangerously-skip-permissions`` to ``claude``)
* ``AMG_RECEIVER_CLAUDE_TIMEOUT_SECONDS``   ``300``
* ``AMG_RECEIVER_LOG_DIR``                  ``~/Library/Logs/messaging-agent``
* ``AMG_RECEIVER_IDLE_WORKER_TTL_SECONDS``  ``300``
* ``AMG_RECEIVER_DEDUPE_CACHE_SIZE``        ``4096``
* ``AMG_RECEIVER_CLAUDE_BIN``               ``claude``
* ``AMG_BASE_URL``                          ``http://127.0.0.1:8080`` (for the MCP wrapper)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ENV_BIND_HOST: Final[str] = "AMG_RECEIVER_BIND_HOST"
ENV_BIND_PORT: Final[str] = "AMG_RECEIVER_BIND_PORT"
ENV_WEBHOOK_SECRET: Final[str] = "AMG_WEBHOOK_SECRET"
ENV_BEARER_TOKEN: Final[str] = "AMG_BEARER_TOKEN"
ENV_AGENT_ID: Final[str] = "AMG_AGENT_ID"
ENV_AGENT_PROMPT_FILE: Final[str] = "AMG_RECEIVER_AGENT_PROMPT_FILE"
ENV_DANGEROUS: Final[str] = "AMG_RECEIVER_DANGEROUS"
ENV_CLAUDE_TIMEOUT_SECONDS: Final[str] = "AMG_RECEIVER_CLAUDE_TIMEOUT_SECONDS"
ENV_LOG_DIR: Final[str] = "AMG_RECEIVER_LOG_DIR"
ENV_IDLE_WORKER_TTL_SECONDS: Final[str] = "AMG_RECEIVER_IDLE_WORKER_TTL_SECONDS"
ENV_DEDUPE_CACHE_SIZE: Final[str] = "AMG_RECEIVER_DEDUPE_CACHE_SIZE"
ENV_CLAUDE_BIN: Final[str] = "AMG_RECEIVER_CLAUDE_BIN"
ENV_BASE_URL: Final[str] = "AMG_BASE_URL"

DEFAULT_BIND_HOST: Final[str] = "127.0.0.1"
DEFAULT_BIND_PORT: Final[int] = 8090
DEFAULT_AGENT_ID: Final[str] = "amg-receiver"
DEFAULT_AGENT_PROMPT_FILE: Final[Path] = Path(
    "~/.config/messaging-agent/agent_prompt.md"
).expanduser()
DEFAULT_CLAUDE_TIMEOUT_SECONDS: Final[int] = 300
DEFAULT_LOG_DIR: Final[Path] = Path("~/Library/Logs/messaging-agent").expanduser()
DEFAULT_IDLE_WORKER_TTL_SECONDS: Final[int] = 300
DEFAULT_DEDUPE_CACHE_SIZE: Final[int] = 4096
DEFAULT_CLAUDE_BIN: Final[str] = "claude"
DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8080"


__all__ = [
    "DEFAULT_AGENT_ID",
    "DEFAULT_AGENT_PROMPT_FILE",
    "DEFAULT_BASE_URL",
    "DEFAULT_BIND_HOST",
    "DEFAULT_BIND_PORT",
    "DEFAULT_CLAUDE_BIN",
    "DEFAULT_CLAUDE_TIMEOUT_SECONDS",
    "DEFAULT_DEDUPE_CACHE_SIZE",
    "DEFAULT_IDLE_WORKER_TTL_SECONDS",
    "DEFAULT_LOG_DIR",
    "ENV_AGENT_ID",
    "ENV_AGENT_PROMPT_FILE",
    "ENV_BASE_URL",
    "ENV_BEARER_TOKEN",
    "ENV_BIND_HOST",
    "ENV_BIND_PORT",
    "ENV_CLAUDE_BIN",
    "ENV_CLAUDE_TIMEOUT_SECONDS",
    "ENV_DANGEROUS",
    "ENV_DEDUPE_CACHE_SIZE",
    "ENV_IDLE_WORKER_TTL_SECONDS",
    "ENV_LOG_DIR",
    "ENV_WEBHOOK_SECRET",
    "ReceiverConfig",
    "ReceiverConfigError",
]


class ReceiverConfigError(ValueError):
    """Raised when required env vars are missing or malformed."""


@dataclass(frozen=True, slots=True)
class ReceiverConfig:
    bind_host: str
    bind_port: int
    webhook_secret: str
    bearer_token: str
    agent_id: str
    agent_prompt_file: Path
    dangerous: bool
    claude_timeout_seconds: int
    log_dir: Path
    idle_worker_ttl_seconds: int
    dedupe_cache_size: int
    claude_bin: str
    base_url: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ReceiverConfig:
        source = env if env is not None else os.environ

        secret = (source.get(ENV_WEBHOOK_SECRET) or "").strip()
        token = (source.get(ENV_BEARER_TOKEN) or "").strip()

        missing: list[str] = []
        if not secret:
            missing.append(ENV_WEBHOOK_SECRET)
        if not token:
            missing.append(ENV_BEARER_TOKEN)
        if missing:
            raise ReceiverConfigError(
                f"Missing required env var(s): {', '.join(missing)}. "
                "Set them in ~/.config/messaging-agent/.env (mode 0600) or the "
                "process environment before launching the receiver."
            )

        bind_host = (source.get(ENV_BIND_HOST) or "").strip() or DEFAULT_BIND_HOST
        bind_port = _parse_int(
            source.get(ENV_BIND_PORT), DEFAULT_BIND_PORT, ENV_BIND_PORT, minimum=1, maximum=65535
        )
        agent_id = (source.get(ENV_AGENT_ID) or "").strip() or DEFAULT_AGENT_ID
        agent_prompt_file = Path(
            (source.get(ENV_AGENT_PROMPT_FILE) or "").strip() or str(DEFAULT_AGENT_PROMPT_FILE)
        ).expanduser()
        dangerous = _parse_bool(source.get(ENV_DANGEROUS))
        claude_timeout_seconds = _parse_int(
            source.get(ENV_CLAUDE_TIMEOUT_SECONDS),
            DEFAULT_CLAUDE_TIMEOUT_SECONDS,
            ENV_CLAUDE_TIMEOUT_SECONDS,
            minimum=1,
        )
        log_dir = Path((source.get(ENV_LOG_DIR) or "").strip() or str(DEFAULT_LOG_DIR)).expanduser()
        idle_worker_ttl_seconds = _parse_int(
            source.get(ENV_IDLE_WORKER_TTL_SECONDS),
            DEFAULT_IDLE_WORKER_TTL_SECONDS,
            ENV_IDLE_WORKER_TTL_SECONDS,
            minimum=1,
        )
        dedupe_cache_size = _parse_int(
            source.get(ENV_DEDUPE_CACHE_SIZE),
            DEFAULT_DEDUPE_CACHE_SIZE,
            ENV_DEDUPE_CACHE_SIZE,
            minimum=1,
        )
        claude_bin = (source.get(ENV_CLAUDE_BIN) or "").strip() or DEFAULT_CLAUDE_BIN
        base_url = (source.get(ENV_BASE_URL) or "").strip() or DEFAULT_BASE_URL

        return cls(
            bind_host=bind_host,
            bind_port=bind_port,
            webhook_secret=secret,
            bearer_token=token,
            agent_id=agent_id,
            agent_prompt_file=agent_prompt_file,
            dangerous=dangerous,
            claude_timeout_seconds=claude_timeout_seconds,
            log_dir=log_dir,
            idle_worker_ttl_seconds=idle_worker_ttl_seconds,
            dedupe_cache_size=dedupe_cache_size,
            claude_bin=claude_bin,
            base_url=base_url,
        )


def _parse_int(
    raw: str | None,
    default: int,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value_str = (raw or "").strip()
    if not value_str:
        return default
    try:
        value = int(value_str)
    except ValueError as exc:
        raise ReceiverConfigError(f"{name} must be an integer; got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ReceiverConfigError(f"{name} must be >= {minimum}; got {value}")
    if maximum is not None and value > maximum:
        raise ReceiverConfigError(f"{name} must be <= {maximum}; got {value}")
    return value


def _parse_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}
