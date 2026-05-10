"""Wrapper config loader.

Mirrors the load/get/reset pattern used by the Python adapter's
``amc/core/auth.py``. Validation runs only when :func:`load_config` is
called explicitly (typically from the entrypoint). Importing this module
does not read or validate env vars — that lets unit tests load the module
without needing the production env set.

Per spec §5.6 the wrapper takes three env vars:
  - ``AMC_BASE_URL``     (optional, default ``http://127.0.0.1:8080``)
  - ``AMC_BEARER_TOKEN`` (required)
  - ``AMC_AGENT_ID``     (required)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

ENV_BASE_URL: Final[str] = "AMC_BASE_URL"
ENV_BEARER_TOKEN: Final[str] = "AMC_BEARER_TOKEN"
ENV_AGENT_ID: Final[str] = "AMC_AGENT_ID"

DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8080"

__all__ = [
    "DEFAULT_BASE_URL",
    "ENV_AGENT_ID",
    "ENV_BASE_URL",
    "ENV_BEARER_TOKEN",
    "WrapperConfig",
    "WrapperConfigError",
    "get_config",
    "load_config",
    "reset_config",
]


class WrapperConfigError(Exception):
    """Raised when required env vars are missing or malformed."""


@dataclass(frozen=True)
class WrapperConfig:
    base_url: str
    bearer_token: str
    agent_id: str


_configured: WrapperConfig | None = None


def load_config(env: dict[str, str | None] | None = None) -> WrapperConfig:
    """Read the three ``AMC_*`` env vars, validate, and cache.

    If ``env`` is omitted, ``os.environ`` is used. Pass an explicit env in
    tests. Raises :class:`WrapperConfigError` if any required var is missing
    or empty after trimming.
    """
    source: dict[str, str | None]
    source = dict(os.environ) if env is None else env

    base_url_raw = source.get(ENV_BASE_URL)
    token_raw = source.get(ENV_BEARER_TOKEN)
    agent_raw = source.get(ENV_AGENT_ID)

    base_url = (base_url_raw or "").strip() or DEFAULT_BASE_URL
    bearer_token = (token_raw or "").strip()
    agent_id = (agent_raw or "").strip()

    missing: list[str] = []
    if not bearer_token:
        missing.append(ENV_BEARER_TOKEN)
    if not agent_id:
        missing.append(ENV_AGENT_ID)

    if missing:
        raise WrapperConfigError(
            f"Missing required env var(s): {', '.join(missing)}. "
            "Set them before launching the MCP wrapper."
        )

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise WrapperConfigError(f"{ENV_BASE_URL} is not a valid URL: {base_url!r}")

    global _configured
    _configured = WrapperConfig(
        base_url=base_url,
        bearer_token=bearer_token,
        agent_id=agent_id,
    )
    return _configured


def get_config() -> WrapperConfig:
    """Return the cached config. Raises if :func:`load_config` has not run."""
    if _configured is None:
        raise WrapperConfigError(
            "Wrapper config has not been loaded. Call load_config() before get_config()."
        )
    return _configured


def reset_config() -> None:
    """Test-only: clear the cached config."""
    global _configured
    _configured = None
