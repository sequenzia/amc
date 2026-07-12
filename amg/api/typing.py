"""``POST /typing`` — best-effort typing indicator (spec §5.1, §7.4.6).

The agent posts ``{"channel_id": "..."}`` and the adapter fans the request
out to the appropriate platform connector to emit a typing indicator. The
endpoint is *best-effort*: per spec §7.4.6 the response is always
``204 No Content`` regardless of whether the platform accepted, ignored, or
errored on the indicator. Failures (channel not found, connector exception,
discord 5xx) are logged at WARN but never surface to the caller.

Routing
-------
Source is inferred from the ``channel_id`` prefix the same way
:mod:`amg.api.messages_send` does:

* ``discord:dm:<id>`` / ``discord:channel:<id>`` → Discord
* anything else → iMessage (E.164 phone number or chat GUID)

We deliberately do NOT perform a ``channels`` table lookup. The endpoint is
best-effort and the spec calls out "Returns 204 even if the platform ignored
it"; a 404 would leak channel state into a fire-and-forget surface.

Discord
~~~~~~~
The injected :class:`DiscordTypingClient` exposes ``trigger_typing(channel_id)``.
The default production wiring constructs a tiny adapter around the running
:class:`amg.connectors.discord.connector.DiscordConnector` that resolves the
channel via :func:`parse_channel_id` + ``client.get_channel`` (with a
``PartialMessageable`` fallback) and calls ``await channel.trigger_typing()``.
Tests inject a fake that records the calls.

Any exception raised by the trigger — Discord HTTP error, channel not in
cache, connector unavailable — is caught and logged. The 204 is still
returned.

iMessage (Phase 2 placeholder)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
iMessage has no OS-level "typing indicator emit" API path that AMG can
trigger from outside the Messages app. This is a deliberate no-op in Phase 1
and is left as a documented placeholder for Phase 2 should an AppleScript
or private-API shim become viable. The route still returns 204.

Wiring
------
The route reads its Discord typing client through a process-wide module
cache set by the app at startup (:func:`configure_discord_typing_client`).
``None`` is a valid configured value — it means Discord typing is
intentionally disabled (e.g. Discord-less deployment) and the Discord branch
becomes a no-op. ``reset_discord_typing_client`` clears the cache between
tests.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from amg.core.auth import require_bearer
from amg.core.envelope import Source

__all__ = [
    "DiscordTypingClient",
    "TypingRequest",
    "configure_discord_typing_client",
    "get_configured_discord_typing_client",
    "reset_discord_typing_client",
    "router",
]


_log = structlog.get_logger("amg.api.typing")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DiscordTypingClient(Protocol):
    """Minimal contract the typing route needs from the Discord side.

    Implementations resolve the AMG-shaped ``channel_id`` to a Discord
    channel and invoke ``await channel.trigger_typing()``. The method must
    be async and should swallow Discord-side errors itself if the caller
    cares about exception isolation — but the typing route also wraps the
    call in its own try/except so either layer is safe.
    """

    async def trigger_typing(self, channel_id: str) -> None:
        """Emit a Discord typing indicator on ``channel_id``."""
        ...


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TypingRequest(BaseModel):
    """Body of ``POST /typing`` (spec §7.4.6)."""

    channel_id: str = Field(
        ...,
        min_length=1,
        description="AMG-shaped channel id (e.g. 'discord:dm:<id>' or '+15551234567').",
    )


# ---------------------------------------------------------------------------
# Module-level caches set at startup
# ---------------------------------------------------------------------------


_discord_typing_client: DiscordTypingClient | None = None
_discord_typing_configured: bool = False


def configure_discord_typing_client(client: DiscordTypingClient | None) -> None:
    """Install the process-wide :class:`DiscordTypingClient`.

    Pass ``None`` to explicitly disable the Discord branch (e.g. an adapter
    deployment that runs without the Discord connector). The route will
    still return 204; the no-op is logged at DEBUG.
    """

    global _discord_typing_client, _discord_typing_configured
    _discord_typing_client = client
    _discord_typing_configured = True


def get_configured_discord_typing_client() -> DiscordTypingClient | None:
    """Return the configured typing client, or ``None`` when disabled.

    Returns ``None`` both when startup has not run *and* when startup
    explicitly configured the client to ``None``. The route treats both
    cases the same — best-effort, no exception.
    """

    return _discord_typing_client


def reset_discord_typing_client() -> None:
    """Drop the cached typing client (test isolation)."""

    global _discord_typing_client, _discord_typing_configured
    _discord_typing_client = None
    _discord_typing_configured = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Channel id prefix for source inference. Mirrors ``amg.api.messages_send``:
# ``discord:`` → Discord, anything else (E.164 phone or chat GUID) → iMessage.
_DISCORD_PREFIX: Final[str] = "discord:"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(tags=["typing"])


@router.post(
    "/typing",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_bearer)],
    summary="Best-effort typing indicator emit",
)
async def emit_typing(payload: TypingRequest) -> Response:
    """Route the typing request to the appropriate platform connector.

    Always returns ``204 No Content``:

    * Discord: invokes ``DiscordTypingClient.trigger_typing(channel_id)``.
      Any exception is logged at WARN and swallowed.
    * iMessage: no-op (Phase 2 placeholder). Logged at DEBUG.
    * Discord requested but no client configured: logged at DEBUG, no-op.

    The empty body is intentional — 204 responses must not have a body
    (RFC 9110 §15.3.5).
    """
    source = _infer_source(payload.channel_id)

    if source is Source.DISCORD:
        await _emit_discord(payload.channel_id)
    else:
        # iMessage Phase 2 placeholder — no AppleScript/private-API path
        # to emit a typing indicator from outside the Messages app.
        _log.debug(
            "typing_imessage_noop",
            channel_id=payload.channel_id,
        )

    # 204 No Content: empty body. ``Response`` (rather than ``JSONResponse``)
    # is required so FastAPI does not attach a JSON content-type / body.
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_source(channel_id: str) -> Source:
    """Map a channel id to its :class:`Source` by prefix.

    Mirrors :func:`amg.api.messages_send._infer_source`. ``discord:...`` →
    Discord, everything else → iMessage.
    """
    if channel_id.startswith(_DISCORD_PREFIX):
        return Source.DISCORD
    return Source.IMESSAGE


async def _emit_discord(channel_id: str) -> None:
    """Best-effort Discord typing emit.

    Swallows every exception (including channel-not-found, network errors,
    and the entirely-unconfigured case) and logs at WARN. The route's 204
    response does not depend on this succeeding.
    """
    client = get_configured_discord_typing_client()
    if client is None:
        _log.debug(
            "typing_discord_noop_no_client",
            channel_id=channel_id,
        )
        return

    try:
        await client.trigger_typing(channel_id)
    except Exception as exc:  # noqa: BLE001 — best-effort, swallow everything
        # WARN (not ERROR) per task description: best-effort surface, the
        # caller never sees this, and a noisy ERROR would mislead operators.
        _log.warning(
            "typing_discord_failed",
            channel_id=channel_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
