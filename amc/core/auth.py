"""Bearer-token authentication and per-agent identification for the AMC adapter.

Spec references: §6.2 Authentication, §7.4 header table, §7.4.12 standard error envelope.

Two FastAPI dependencies:

- :func:`require_bearer` — validates ``Authorization: Bearer <token>`` against the
  configured server token using a constant-time comparison. Applied globally so no
  endpoint is anonymous (per spec §6.2).
- :func:`require_agent_id` — composable dependency that extracts ``X-Agent-ID`` and
  raises ``400 AGENT_ID_REQUIRED`` if missing. Use only on routes that need per-agent
  cursor isolation (§7.4 header table).

The bearer token is loaded once at process startup via :func:`load_bearer_token`,
which reads ``~/.config/messaging-agent/.env`` with ``python-dotenv``. If the env var
``AMC_BEARER_TOKEN`` is missing or empty, the loader raises :class:`RuntimeError` so
the adapter refuses to start.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Final

from dotenv import dotenv_values
from fastapi import Header, HTTPException, status

__all__ = [
    "DEFAULT_ENV_PATH",
    "configure_bearer_token",
    "get_configured_bearer_token",
    "load_bearer_token",
    "require_agent_id",
    "require_bearer",
    "reset_bearer_token",
]

DEFAULT_ENV_PATH: Final[Path] = Path("~/.config/messaging-agent/.env").expanduser()
_TOKEN_ENV_VAR: Final[str] = "AMC_BEARER_TOKEN"
_AGENT_ID_HEADER: Final[str] = "X-Agent-ID"

# Module-level cache. Set once at startup by load_bearer_token() /
# configure_bearer_token(); read by require_bearer().
_configured_token: str | None = None


def _build_error(code: str, message: str) -> dict[str, dict[str, str]]:
    """Return the standard error envelope body (spec §7.4.12)."""
    return {"error": {"code": code, "message": message}}


def load_bearer_token(env_path: Path | None = None) -> str:
    """Load ``AMC_BEARER_TOKEN`` and cache it for :func:`require_bearer`.

    Reads first from ``env_path`` (defaulting to ``~/.config/messaging-agent/.env``)
    and falls back to the process environment. Raises :class:`RuntimeError` with a
    clear message if the token is missing or empty so the adapter refuses to start.

    Returns the loaded token. Safe to call multiple times; subsequent calls overwrite
    the cached value.
    """
    path = env_path if env_path is not None else DEFAULT_ENV_PATH

    file_values: dict[str, str | None] = {}
    if path.exists():
        file_values = dotenv_values(path)

    token = file_values.get(_TOKEN_ENV_VAR) or os.environ.get(_TOKEN_ENV_VAR)
    if not token:
        raise RuntimeError(
            f"{_TOKEN_ENV_VAR} is not set. The AMC adapter refuses to start without a "
            f"bearer token. Set {_TOKEN_ENV_VAR} in {path} (mode 0600) or in the "
            f"process environment."
        )

    configure_bearer_token(token)
    return token


def configure_bearer_token(token: str) -> None:
    """Set the cached token directly (useful for tests and explicit overrides)."""
    if not token:
        raise ValueError("Bearer token must be a non-empty string.")
    global _configured_token
    _configured_token = token


def get_configured_bearer_token() -> str:
    """Return the cached token, or raise if :func:`load_bearer_token` has not run."""
    if _configured_token is None:
        raise RuntimeError(
            "Bearer token has not been loaded. Call load_bearer_token() at startup "
            "or configure_bearer_token() in tests."
        )
    return _configured_token


def reset_bearer_token() -> None:
    """Clear the cached token. Intended for test isolation."""
    global _configured_token
    _configured_token = None


def _parse_bearer(header_value: str) -> str | None:
    """Extract the token portion of an ``Authorization: Bearer <token>`` header.

    Returns ``None`` for empty headers, malformed schemes, or empty tokens. The
    ``Bearer`` scheme is case-insensitive and surrounding whitespace is tolerated.
    """
    stripped = header_value.strip()
    if not stripped:
        return None

    parts = stripped.split(None, 1)
    if len(parts) != 2:
        return None

    scheme, token = parts
    if scheme.lower() != "bearer":
        return None

    token = token.strip()
    if not token:
        return None

    return token


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: validate ``Authorization: Bearer <token>``.

    Raises ``HTTPException(401, code=UNAUTHORIZED)`` when the header is missing,
    malformed, or does not match the configured token. Token comparison uses
    :func:`secrets.compare_digest` so it is resistant to timing attacks.
    """
    expected = get_configured_bearer_token()

    presented: str | None = None
    if authorization is not None:
        presented = _parse_bearer(authorization)

    # Always run compare_digest so the failure path takes the same time regardless of
    # whether the header was missing or just wrong.
    candidate = presented if presented is not None else ""
    valid = secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))

    if presented is None or not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_build_error("UNAUTHORIZED", "Missing or invalid bearer token."),
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_agent_id(
    x_agent_id: str | None = Header(default=None, alias=_AGENT_ID_HEADER),
) -> str:
    """FastAPI dependency: extract and validate ``X-Agent-ID``.

    Returns the trimmed agent id on success. Raises
    ``HTTPException(400, code=AGENT_ID_REQUIRED)`` when the header is missing or
    blank. Compose with :func:`require_bearer` on routes that need per-agent cursor
    isolation (spec §7.4 header table).
    """
    if x_agent_id is None or not x_agent_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_build_error("AGENT_ID_REQUIRED", "X-Agent-ID header is required."),
        )
    return x_agent_id.strip()
