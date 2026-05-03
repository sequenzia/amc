"""``GET /healthz`` — liveness, connector states, queue depths (spec §7.4.10).

Public surface
--------------
* :data:`router` — :class:`fastapi.APIRouter` with no prefix; the route is
  literally ``/healthz``.
* Connector registry (read by the route, written by the adapter at startup):
    * :func:`register_connector`
    * :func:`unregister_connector`
    * :func:`reset_connector_registry`
    * :func:`get_registered_connectors`
* Start-time hooks (used to compute ``uptime_seconds``):
    * :func:`record_start_time`
    * :func:`get_recorded_start_time`
    * :func:`reset_start_time`
* Version cache (read once at startup from ``pyproject.toml``):
    * :func:`load_version`
    * :func:`get_cached_version`
    * :func:`reset_cached_version`

Connector state mapping
-----------------------
For each known source (``imessage``, ``discord``):

* No connector registered → ``"disabled"`` (the operator intentionally turned
  the connector off in this deployment, or it has not been wired yet).
* Connector registered, ``connector.degraded`` is ``True`` → ``"degraded"``.
* Connector registered, ``connector.degraded`` is falsy → ``"ok"``.

Per spec §5.1 error handling, a connector that hits a permanent platform
failure (FDA missing, bot token revoked, AppleScript automation denied)
flips its own ``degraded`` flag to ``True``. The route only reads the flag
— it never mutates connector state.

``last_message_at``
-------------------
The most recent ``messages.created_at`` for that ``source``. Returned as the
exact string SQLite stored (ISO 8601 with milliseconds and ``Z`` suffix),
or ``None`` when no messages have been recorded yet for that source.

``webhook_queue``
-----------------
``{"pending": N, "dead": M}`` — counts from
``SELECT status, COUNT(*) FROM webhook_deliveries GROUP BY status``. The
``delivered`` bucket is intentionally omitted from the response (spec §7.4.10
shape only enumerates ``pending`` and ``dead``); operators tail the
delivered count via the structured logs.

App wiring (out of scope for this module)
-----------------------------------------
``amc/app.py`` is owned by a sibling task in this wave, so this module
deliberately exposes setup hooks rather than mutating the app builder
itself. The expected wiring at adapter startup is::

    from amc.api import healthz

    healthz.record_start_time(time.monotonic())
    healthz.load_version()
    healthz.register_connector("discord", discord_connector)
    if imessage_connector is not None:
        healthz.register_connector("imessage", imessage_connector)

    app.state.start_time = healthz.get_recorded_start_time()
    app.include_router(healthz.router)

The route prefers ``request.app.state.start_time`` when set (it lets a test
harness construct a fresh app per case) and falls back to the module-level
cache otherwise.
"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from amc.core.auth import require_bearer

__all__ = [
    "ConnectorLike",
    "KNOWN_SOURCES",
    "get_cached_version",
    "get_recorded_start_time",
    "get_registered_connectors",
    "load_version",
    "record_start_time",
    "register_connector",
    "reset_cached_version",
    "reset_connector_registry",
    "reset_start_time",
    "router",
    "unregister_connector",
]

# Sources the adapter knows about. The shape of the response always exposes
# both keys, even when one is "disabled", so MCP clients don't have to
# special-case missing keys (spec §7.4.10 example).
KNOWN_SOURCES: Final[tuple[str, str]] = ("imessage", "discord")

# Repo root resolved from this file's location: amc/api/healthz.py → repo/.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEFAULT_PYPROJECT_PATH: Final[Path] = _REPO_ROOT / "pyproject.toml"


@runtime_checkable
class ConnectorLike(Protocol):
    """Structural type for objects the registry accepts.

    The route only reads ``connector.degraded`` — every Phase 1/2 connector
    (Discord, iMessage) exposes this attribute per spec §5.1 error
    handling. Tests pass a tiny stand-in object with the same field.
    """

    degraded: bool


# ---------------------------------------------------------------------------
# Connector registry
# ---------------------------------------------------------------------------

# Module-level registry. The adapter populates it at startup; the route reads
# it on every request. ``async_sessionmaker`` is the only other shared piece
# of state, but that one comes off ``request.app.state`` so we don't double
# up here.
_connector_registry: dict[str, ConnectorLike] = {}


def register_connector(source: str, connector: ConnectorLike) -> None:
    """Register ``connector`` for ``source`` (``"imessage"`` or ``"discord"``).

    Raises ``ValueError`` for an unknown source so a typo at wiring time
    surfaces immediately rather than silently making the connector
    appear disabled in ``/healthz`` forever.
    """
    if source not in KNOWN_SOURCES:
        raise ValueError(
            f"Unknown source {source!r}; expected one of {KNOWN_SOURCES}."
        )
    _connector_registry[source] = connector


def unregister_connector(source: str) -> None:
    """Remove ``source`` from the registry. No-op if it wasn't registered."""
    _connector_registry.pop(source, None)


def reset_connector_registry() -> None:
    """Clear all registered connectors. Intended for test isolation."""
    _connector_registry.clear()


def get_registered_connectors() -> dict[str, ConnectorLike]:
    """Return a shallow copy of the current registry (for diagnostics / tests)."""
    return dict(_connector_registry)


# ---------------------------------------------------------------------------
# Start time
# ---------------------------------------------------------------------------

# Cached at startup; reset between tests. ``time.monotonic()`` is preferred
# over ``time.time()`` for uptime — it isn't affected by wall-clock jumps
# (NTP, daylight saving, manual ``date`` changes).
_recorded_start_time: float | None = None


def record_start_time(start_time: float | None = None) -> float:
    """Record the adapter start time and return it.

    When ``start_time`` is ``None`` we capture ``time.monotonic()`` now;
    callers may pass a pre-captured value for tests or when start time was
    measured before this module was imported.
    """
    global _recorded_start_time
    _recorded_start_time = start_time if start_time is not None else time.monotonic()
    return _recorded_start_time


def get_recorded_start_time() -> float | None:
    """Return the cached start time (or ``None`` if never recorded)."""
    return _recorded_start_time


def reset_start_time() -> None:
    """Clear the recorded start time. Intended for test isolation."""
    global _recorded_start_time
    _recorded_start_time = None


# ---------------------------------------------------------------------------
# Version (read once from pyproject.toml)
# ---------------------------------------------------------------------------

_cached_version: str | None = None


def load_version(pyproject_path: Path | None = None) -> str:
    """Read ``project.version`` from ``pyproject.toml`` and cache it.

    Defaults to the repo-root pyproject. Tests can pass an explicit path
    pointing at a fixture file. The cached value is returned by
    :func:`get_cached_version` and surfaced in the ``version`` field.

    Raises ``RuntimeError`` if ``project.version`` is missing or non-string
    so the adapter refuses to start with a broken pyproject — consistent
    with how :func:`amc.core.auth.load_bearer_token` refuses to start
    without a token.
    """
    path = pyproject_path if pyproject_path is not None else _DEFAULT_PYPROJECT_PATH
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project")
    if not isinstance(project, dict):
        raise RuntimeError(
            f"pyproject.toml at {path}: missing or non-table [project] section."
        )
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError(
            f"pyproject.toml at {path}: project.version must be a non-empty string."
        )
    global _cached_version
    _cached_version = version
    return version


def get_cached_version() -> str:
    """Return the cached version, or raise if :func:`load_version` has not run."""
    if _cached_version is None:
        raise RuntimeError(
            "Version has not been loaded. Call load_version() at startup."
        )
    return _cached_version


def reset_cached_version() -> None:
    """Clear the cached version. Intended for test isolation."""
    global _cached_version
    _cached_version = None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

router = APIRouter(tags=["health"])


def _resolve_uptime_seconds(request: Request) -> int:
    """Compute uptime as ``int(monotonic_now - start_time)``.

    Resolution order for the start time:
    1. ``request.app.state.start_time`` — preferred so tests with a fresh
       app per case don't leak module state across runs.
    2. The module-level :func:`record_start_time` cache.
    3. Fall back to ``0`` (never recorded) so the endpoint stays
       responsive — health is more important than absolute precision when
       the adapter forgot to wire start_time.
    """
    start = getattr(request.app.state, "start_time", None)
    if start is None:
        start = _recorded_start_time
    if start is None:
        return 0
    delta = time.monotonic() - start
    return max(0, int(delta))


def _connector_state(source: str) -> str:
    """Map registry membership + ``degraded`` flag to the spec state string."""
    connector = _connector_registry.get(source)
    if connector is None:
        return "disabled"
    return "degraded" if getattr(connector, "degraded", False) else "ok"


async def _last_message_at(
    factory: async_sessionmaker, source: str
) -> str | None:
    """Return ``max(messages.created_at) WHERE source = ?`` or ``None``.

    Each call opens its own short transaction so the route stays usable
    even when the writer is mid-flight on a long INSERT (SQLite WAL mode).
    """
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT MAX(created_at) FROM messages WHERE source = :source"
            ),
            {"source": source},
        )
        row = result.first()
    if row is None:
        return None
    value = row[0]
    if not isinstance(value, str) or not value:
        return None
    return value


async def _webhook_queue_counts(factory: async_sessionmaker) -> dict[str, int]:
    """Return ``{"pending": N, "dead": M}`` from a single GROUP BY query.

    Buckets that aren't present in the result default to ``0``. The
    ``delivered`` bucket is dropped — spec §7.4.10 only documents the
    pending/dead pair.
    """
    counts: dict[str, int] = {"pending": 0, "dead": 0}
    async with factory() as session:
        result = await session.execute(
            text("SELECT status, COUNT(*) FROM webhook_deliveries GROUP BY status")
        )
        rows = result.all()
    for status_value, count in rows:
        if status_value in counts:
            counts[status_value] = int(count)
    return counts


def _resolve_session_factory(request: Request) -> async_sessionmaker:
    """Pull the async session factory off ``request.app.state``.

    Mirrors the pattern in :mod:`amc.api.messages_context` so the route
    keeps the same wiring contract as the rest of the Phase 1 endpoints.
    Tests put the factory on ``app.state.session_factory`` before mounting
    the router.
    """
    factory: async_sessionmaker | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:
        raise RuntimeError(
            "request.app.state.session_factory is not configured. "
            "The adapter app must wire an async_sessionmaker before serving "
            "/healthz."
        )
    return factory


@router.get(
    "/healthz",
    dependencies=[Depends(require_bearer)],
    summary="Liveness, connector states, and webhook queue depths.",
)
async def get_healthz(request: Request) -> dict[str, Any]:
    """Return the spec §7.4.10 health envelope.

    Bearer is required (router-level dep). No ``X-Agent-ID`` because
    ``/healthz`` is operator-facing — per spec §6.2 / §7.4 header table the
    operator role does not carry a per-agent header.
    """
    factory = _resolve_session_factory(request)

    connector_block: dict[str, dict[str, Any]] = {}
    for source in KNOWN_SOURCES:
        connector_block[source] = {
            "state": _connector_state(source),
            "last_message_at": await _last_message_at(factory, source),
        }

    webhook_block = await _webhook_queue_counts(factory)

    return {
        "status": "ok",
        "uptime_seconds": _resolve_uptime_seconds(request),
        "connectors": connector_block,
        "webhook_queue": webhook_block,
        "version": get_cached_version(),
    }
