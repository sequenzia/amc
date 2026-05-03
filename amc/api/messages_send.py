"""``POST /messages/send`` — outbound message endpoint (spec §5.2, §7.4.5).

The agent posts a :class:`amc.core.envelope.SendMessageRequest`; the route:

1. Short-circuits on a same-hash idempotency replay (cached body returned with
   ``Idempotency-Replayed: true``); a different-hash collision is surfaced as
   ``422 IDEMPOTENCY_KEY_REUSE``.
2. Resolves ``(source, channel_id)`` against the ``channels`` table. The
   ``channel_id`` is platform-prefixed (``discord:dm:<id>``,
   ``discord:channel:<id>``, or an iMessage E.164 / chat GUID); the source is
   inferred from the prefix. A row that was never observed inbound returns
   ``404 CHANNEL_NOT_FOUND`` — the adapter only sends to channels it has seen.
3. Acquires one token from the per-channel token bucket. A rejection is
   translated to ``429 RATE_LIMITED`` with a ``Retry-After`` header.
4. Resolves attachments per platform (see "Attachments policy" below).
5. Routes to the configured platform connector:
   * ``discord`` → :meth:`amc.connectors.discord.connector.DiscordConnector.send`.
     Errors are mapped to ``502 PLATFORM_AUTH`` (401/403) or
     ``502 PLATFORM_SEND_FAILED`` (any other discord HTTP error). 5xx that
     ``discord.py`` retries internally never reach us — only the terminal
     outcome surfaces.
   * ``imessage`` → :meth:`amc.connectors.imessage.connector.ImessageConnector.send`.
     A failed AppleScript send (``ok=False`` from the underlying sender after
     all retries) becomes ``502 PLATFORM_SEND_FAILED``.
6. Persists an outbound row in ``messages`` with ``direction='outbound'``,
   ``allowlist_status='outbound'`` (per the schema CHECK), and
   ``delivery_status='sent'`` (the column added by migration 002 records the
   send state machine), keyed by a fresh ``msg_<ULID>`` id (the connector's
   platform-native id is captured in ``raw_json``).
7. Returns ``{"message_id": "msg_...", "sent_at": "<rfc3339>"}``.

Attachments policy
------------------
:class:`amc.core.envelope.SendAttachmentInput` permits ``url`` *or* ``path``.

* **iMessage** (#51): both ``url`` and ``path`` are honored.

  * ``url`` must be the adapter-hosted form (``/attachments/<att_id>`` or
    ``http(s)://.../attachments/<att_id>``). The route looks up
    ``attachments.bytes_path`` for the id and passes the local path to the
    AppleScript sender. A missing or retention-deleted row returns
    ``404 MESSAGE_NOT_FOUND`` (matching :mod:`amc.api.attachments_get`
    semantics).
  * ``path`` must be an absolute filesystem path; relative paths are
    rejected with ``400 VALIDATION_FAILED``. The path is otherwise trusted
    (the operator owns the FS) — no MIME / size pre-check, AppleScript
    will surface a failure if the file is missing.
  * Only the **first** attachment is sent; v1 AppleScript supports a single
    attachment per send. (The route does not pre-reject multi-attach
    requests; surplus attachments are dropped with a WARN log.)

* **Discord**: outbound attachments still land in Phase 1's stub.

  * The Discord connector's ``send`` signature takes
    :class:`discord.File` objects, not arbitrary URLs/paths; wiring URL
    pre-fetch + size check requires a follow-on task.
  * For now, any Discord attachment in the request returns
    ``413 ATTACHMENT_TOO_LARGE_FOR_PLATFORM`` with a message noting both
    the unimplemented passthrough AND the eventual 25 MB hard limit
    (spec §5.5 edge case).

Wiring
------
The route reads its DB session factory, rate limiter, and platform
connectors through process-wide module caches set by the app at startup:

* :func:`configure_session_factory` — :class:`async_sessionmaker[AsyncSession]`
  bound to the migrated SQLite store.
* :func:`configure_rate_limiter` — :class:`amc.core.rate_limit.RateLimiter`
  built from env config.
* :func:`configure_discord_connector` — the running
  :class:`amc.connectors.discord.connector.DiscordConnector` instance. May be
  ``None`` if Discord is intentionally disabled in this deployment; a Discord
  send in that case returns ``502 PLATFORM_SEND_FAILED``.
* :func:`configure_imessage_connector` — the running
  :class:`amc.connectors.imessage.connector.ImessageConnector` instance. May
  be ``None`` if iMessage is intentionally disabled in this deployment; an
  iMessage send in that case returns ``502 PLATFORM_SEND_FAILED``.

The corresponding ``reset_*`` functions clear the caches between tests.

This module purposefully does NOT depend on
:class:`amc.core.message_sink.MessageSink`. The sink ties cursor advancement to
inbound persistence; outbound sends do not advance the connector cursor (the
spec §5.1 cursor is for the inbound poll path). A direct UPSERT is simpler and
matches the rest of this module's hand-written SQL style.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Protocol, runtime_checkable

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ulid import ULID

from amc.connectors.discord.connector import (
    AmcDiscordErrorCode,
    DiscordConnector,
)
from amc.connectors.discord.connector import (
    SendResult as DiscordSendResult,
)
from amc.connectors.imessage.applescript import SendResult as ImessageSendResult
from amc.core.auth import require_bearer
from amc.core.envelope import SendAttachmentInput, SendMessageRequest, Source
from amc.core.errors import AMCError, ErrorCode
from amc.core.idempotency import IdempotencyContext, idempotent
from amc.core.rate_limit import RateLimitedError, RateLimiter

__all__ = [
    "ImessageConnectorLike",
    "configure_discord_connector",
    "configure_imessage_connector",
    "configure_rate_limiter",
    "configure_session_factory",
    "get_configured_discord_connector",
    "get_configured_imessage_connector",
    "get_configured_rate_limiter",
    "get_configured_session_factory",
    "reset_discord_connector",
    "reset_imessage_connector",
    "reset_rate_limiter",
    "reset_session_factory",
    "router",
]


@runtime_checkable
class ImessageConnectorLike(Protocol):
    """Structural type for the iMessage connector's outbound surface.

    The route only needs ``send``; declaring a Protocol (rather than
    importing the concrete :class:`ImessageConnector`) lets tests
    substitute a slimmer fake without standing up a full poller.
    """

    async def send(
        self,
        channel_id: str,
        text_body: str,
        *,
        reply_to: str | None = None,
        attachments: Any | None = None,
    ) -> ImessageSendResult:
        ...


_log = structlog.get_logger("amc.api.messages_send")


# ---------------------------------------------------------------------------
# Module-level caches set at startup
# ---------------------------------------------------------------------------


_session_factory: async_sessionmaker[AsyncSession] | None = None
_rate_limiter: RateLimiter | None = None
_discord_connector: DiscordConnector | None = None
_imessage_connector: ImessageConnectorLike | None = None


def configure_session_factory(factory: async_sessionmaker[AsyncSession]) -> None:
    """Install the process-wide async session factory the route uses."""

    global _session_factory
    _session_factory = factory


def get_configured_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the configured session factory; raise if startup hasn't run."""

    if _session_factory is None:
        raise RuntimeError(
            "messages_send session factory not configured; "
            "call configure_session_factory() at startup."
        )
    return _session_factory


def reset_session_factory() -> None:
    """Drop the cached session factory (test isolation)."""

    global _session_factory
    _session_factory = None


def configure_rate_limiter(limiter: RateLimiter) -> None:
    """Install the process-wide per-channel :class:`RateLimiter`."""

    global _rate_limiter
    _rate_limiter = limiter


def get_configured_rate_limiter() -> RateLimiter:
    """Return the configured rate limiter; raise if startup hasn't run."""

    if _rate_limiter is None:
        raise RuntimeError(
            "messages_send rate limiter not configured; "
            "call configure_rate_limiter() at startup."
        )
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Drop the cached rate limiter (test isolation)."""

    global _rate_limiter
    _rate_limiter = None


def configure_discord_connector(connector: DiscordConnector | None) -> None:
    """Install the running :class:`DiscordConnector` (or ``None`` to disable)."""

    global _discord_connector
    _discord_connector = connector


def get_configured_discord_connector() -> DiscordConnector | None:
    """Return the configured connector. ``None`` means Discord send is disabled."""

    return _discord_connector


def reset_discord_connector() -> None:
    """Drop the cached connector (test isolation)."""

    global _discord_connector
    _discord_connector = None


def configure_imessage_connector(
    connector: ImessageConnectorLike | None,
) -> None:
    """Install the running iMessage connector (or ``None`` to disable)."""

    global _imessage_connector
    _imessage_connector = connector


def get_configured_imessage_connector() -> ImessageConnectorLike | None:
    """Return the configured iMessage connector. ``None`` means iMessage send is disabled."""

    return _imessage_connector


def reset_imessage_connector() -> None:
    """Drop the cached iMessage connector (test isolation)."""

    global _imessage_connector
    _imessage_connector = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Channel id prefixes for source inference. iMessage channel ids are E.164
# phone numbers ("+1...") or chat GUIDs and have no fixed prefix; we identify
# Discord channels by their explicit ``discord:`` prefix and treat anything
# else as iMessage when looking the row up.
_DISCORD_PREFIX: Final[str] = "discord:"


# Match the adapter-hosted attachment URL forms emitted by inbound envelope
# rendering: an absolute path (``/attachments/<id>``) or a full URL whose path
# starts with that prefix (``http(s)://host[:port]/attachments/<id>``). The
# id capture is intentionally permissive on length / alphabet — strict
# pattern enforcement happens in the SELECT against the ``attachments`` table.
_ATTACHMENT_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:https?://[^/]+)?/attachments/(?P<att_id>[^/?#]+)$"
)


# Discord's free-tier upload limit (spec §5.5 edge case). Reserved for the
# follow-on task that wires Discord URL/path attachment passthrough; the
# Phase 1 stub returns 413 for any Discord attachment regardless of size.
DISCORD_ATTACHMENT_MAX_BYTES: Final[int] = 25 * 1024 * 1024


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/messages", tags=["messages"])


# Module-level annotated dep so ``Depends(...)`` doesn't end up in the route
# function defaults (ruff B008) and matches the FastAPI ``Annotated`` style.
_IdemDep = Annotated[IdempotencyContext | None, Depends(idempotent("send"))]


@router.post(
    "/send",
    dependencies=[Depends(require_bearer)],
    summary="Send an outbound message via the configured platform connector",
)
async def send_message(
    payload: SendMessageRequest,
    idem: _IdemDep,
) -> JSONResponse:
    """Validate, route, persist, and return the platform send result.

    The handler does not return a Pydantic model directly because the
    idempotency replay path may have to swap in a previously-cached response
    body verbatim — building a model only to immediately re-serialize would
    add overhead and risk re-canonicalizing the cached payload differently.

    Per spec §7.4.5 the success body is::

        {"message_id": "msg_<ULID>", "sent_at": "2026-04-25T15:32:11.123Z"}
    """

    # -- 1. Resolve source from the channel_id prefix and look up the row ---
    source = _infer_source(payload.channel_id)
    factory = get_configured_session_factory()
    async with factory() as session:
        channel_row = await _load_channel_row(session, source, payload.channel_id)
    if channel_row is None:
        raise AMCError(
            code=ErrorCode.CHANNEL_NOT_FOUND,
            message=(
                f"Channel {payload.channel_id!r} not found. "
                f"The adapter only sends to channels it has previously observed."
            ),
            details={"channel_id": payload.channel_id, "source": source.value},
        )
    channel_type = channel_row["channel_type"]

    # -- 2. Per-channel rate limit ----------------------------------------
    limiter = get_configured_rate_limiter()
    try:
        retry_after = await limiter.acquire(payload.channel_id)
        if retry_after is not None:
            raise RateLimitedError(retry_after)
    except RateLimitedError as exc:
        raise AMCError(
            code=ErrorCode.RATE_LIMITED,
            message="Per-channel rate limit exceeded.",
            headers={"Retry-After": str(max(1, int(exc.retry_after)))},
            details={"channel_id": payload.channel_id},
        ) from exc

    # -- 3. Route to the platform connector -------------------------------
    # Each branch resolves its own attachment shape (Discord still rejects;
    # iMessage now resolves URL→bytes_path or trusts an absolute path) and
    # returns a uniform ``(platform_message_id, ImessageSendResult|None)``
    # tuple — but to keep persistence symmetric the branches just record the
    # platform_message_id and let the shared persist step run.
    sent_at = _utcnow()
    message_id = f"msg_{ULID()}"
    platform_message_id: str | None

    if source is Source.IMESSAGE:
        attachment_path = await _resolve_imessage_attachment(
            payload.attachments, factory
        )
        imessage_connector = get_configured_imessage_connector()
        if imessage_connector is None:
            raise AMCError(
                code=ErrorCode.PLATFORM_SEND_FAILED,
                message="iMessage connector is not configured in this adapter.",
                details={"channel_id": payload.channel_id},
            )

        imessage_result: ImessageSendResult = await imessage_connector.send(
            payload.channel_id,
            payload.text,
            attachments=[attachment_path] if attachment_path is not None else None,
        )
        if not imessage_result.ok:
            raise AMCError(
                code=ErrorCode.PLATFORM_SEND_FAILED,
                message=(
                    f"iMessage send failed after retries: "
                    f"{imessage_result.error or 'unknown error'}"
                ),
                details={
                    "channel_id": payload.channel_id,
                    "platform_detail": imessage_result.raw_stderr,
                },
            )
        # AppleScript does not surface a platform-native message id; record
        # ``None`` so ``raw_json`` is a clean NULL rather than a noisy stub.
        platform_message_id = None
    else:
        # source is DISCORD beyond this point.
        if payload.attachments:
            _reject_discord_attachments_phase1()

        discord_connector = get_configured_discord_connector()
        if discord_connector is None:
            raise AMCError(
                code=ErrorCode.PLATFORM_SEND_FAILED,
                message="Discord connector is not configured in this adapter.",
                details={"channel_id": payload.channel_id},
            )

        discord_result: DiscordSendResult = await discord_connector.send(
            payload.channel_id,
            payload.text,
            reply_to=_extract_reply_to_for_discord(payload.reply_to),
        )
        if not discord_result.ok:
            raise _map_discord_error(discord_result, payload.channel_id)
        platform_message_id = discord_result.message_id

    # -- 4. Persist outbound row -----------------------------------------
    await _persist_outbound(
        session_factory=factory,
        message_id=message_id,
        source=source,
        channel_id=payload.channel_id,
        channel_type=channel_type,
        text_body=payload.text,
        reply_to=payload.reply_to,
        message_ts=sent_at,
        platform_message_id=platform_message_id,
    )

    response_body: dict[str, Any] = {
        "message_id": message_id,
        "sent_at": _format_ts(sent_at),
    }

    # -- 5. Idempotency cache record + race-loser swap -------------------
    if idem is not None:
        await idem.record(status=200, body=response_body)
        if idem.replayed and idem.replayed_body is not None:
            # Race loser: another writer's response is the canonical one.
            return JSONResponse(
                status_code=idem.replayed_status or 200,
                content=json.loads(idem.replayed_body),
            )

    _log.info(
        "message_sent",
        message_id=message_id,
        source=source.value,
        channel_id=payload.channel_id,
        platform_message_id=platform_message_id,
    )
    return JSONResponse(status_code=200, content=response_body)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_source(channel_id: str) -> Source:
    """Map a channel id to its :class:`Source`.

    The channel id format is platform-defined (spec §7.3.1). We treat
    ``discord:...`` as Discord and everything else as iMessage. This matches
    the inverse of :func:`amc.connectors.discord.connector.build_channel_id`.
    """
    if channel_id.startswith(_DISCORD_PREFIX):
        return Source.DISCORD
    return Source.IMESSAGE


async def _load_channel_row(
    session: AsyncSession,
    source: Source,
    channel_id: str,
) -> dict[str, Any] | None:
    """Return the ``channels`` row matching ``(source, channel_id)``, or ``None``."""
    result = await session.execute(
        text(
            "SELECT source, channel_id, channel_type "
            "FROM channels WHERE source = :source AND channel_id = :channel_id"
        ),
        {"source": source.value, "channel_id": channel_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return dict(row)


def _reject_discord_attachments_phase1() -> None:
    """Phase 1: reject Discord outbound attachments with a 413 envelope.

    Discord URL pass-through is not implemented because the connector's
    send signature takes :class:`discord.File` objects, not URLs; a
    follow-on task would fetch + re-host the URL into a File AND enforce
    the platform's 25 MB free-tier upload limit (spec §5.5 edge case).
    Until that lands the route returns a uniform 413 for any Discord
    attachment regardless of size.

    ``ATTACHMENT_TOO_LARGE_FOR_PLATFORM`` is the only canonical attachment
    error code in the spec; the message body explains the actual limitation.
    """
    raise AMCError(
        code=ErrorCode.ATTACHMENT_TOO_LARGE_FOR_PLATFORM,
        message=(
            "Discord attachment passthrough is not implemented in Phase 1. "
            f"Discord enforces a {DISCORD_ATTACHMENT_MAX_BYTES // (1024 * 1024)} "
            "MB upload limit; once passthrough is wired the route will pre-check "
            "and reject oversize uploads. Send text-only for now."
        ),
        status=413,
        details={"source": Source.DISCORD.value},
    )


async def _resolve_imessage_attachment(
    attachments: list[SendAttachmentInput],
    session_factory: async_sessionmaker[AsyncSession],
) -> str | None:
    """Resolve the *first* iMessage outbound attachment to a local path.

    v1 AppleScript supports a single attachment per send; the route picks
    the first attachment in the list and drops the rest with a WARN log
    (so multi-attach callers learn to send sequentially without losing the
    first one).

    Resolution rules:

    * ``url`` matching ``[http(s)://host[:port]]/attachments/<att_id>`` →
      look up ``attachments.bytes_path`` for the id. Missing row, NULL
      ``bytes_path`` (retention-deleted), or the file gone from disk all
      surface as ``404 MESSAGE_NOT_FOUND`` (matching
      :mod:`amc.api.attachments_get`).
    * ``url`` not matching the adapter form → ``400 VALIDATION_FAILED``.
      iMessage's outbound path cannot fetch arbitrary URLs.
    * ``path`` set → must be an absolute filesystem path. Relative paths
      are rejected with ``400 VALIDATION_FAILED``. Existence is *not*
      checked here — the operator owns the FS and AppleScript will
      surface the failure if the file is gone.
    """
    if not attachments:
        return None

    if len(attachments) > 1:
        _log.warning(
            "imessage_multi_attachment_truncated",
            count=len(attachments),
            note="v1 AppleScript template supports a single attachment per send.",
        )

    attachment = attachments[0]

    if attachment.url is not None:
        att_id = _parse_adapter_attachment_url(attachment.url)
        if att_id is None:
            raise AMCError(
                code=ErrorCode.VALIDATION_FAILED,
                message=(
                    "iMessage outbound attachment 'url' must be the "
                    "adapter-hosted form (/attachments/<id>). External URLs "
                    "are not fetched on the outbound path."
                ),
                details={"url": attachment.url},
            )
        return await _resolve_attachment_bytes_path(att_id, session_factory)

    # ``path`` is set (model validator guarantees exactly one of url/path).
    assert attachment.path is not None
    if not os.path.isabs(attachment.path):
        raise AMCError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(
                "iMessage outbound attachment 'path' must be an absolute "
                "filesystem path."
            ),
            details={"path": attachment.path},
        )
    return attachment.path


def _parse_adapter_attachment_url(url: str) -> str | None:
    """Return the ``att_id`` from an adapter-hosted URL, else ``None``."""
    match = _ATTACHMENT_URL_RE.match(url)
    if match is None:
        return None
    return match.group("att_id")


async def _resolve_attachment_bytes_path(
    att_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Look up ``attachments.bytes_path`` for ``att_id``; raise on miss."""
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT bytes_path FROM attachments WHERE id = :id"
                    ),
                    {"id": att_id},
                )
            )
            .mappings()
            .first()
        )

    if row is None or row["bytes_path"] is None:
        # Mirror :mod:`amc.api.attachments_get` — a deleted-row and a
        # retention-purged row collapse to the same 404 envelope so the
        # caller cannot probe whether a deleted attachment ever existed.
        raise AMCError(
            code=ErrorCode.MESSAGE_NOT_FOUND,
            message=f"Attachment {att_id!r} not found.",
            details={"attachment_id": att_id},
        )

    bytes_path: str = row["bytes_path"]
    if not os.path.isfile(bytes_path):
        _log.error(
            "imessage_attachment_bytes_missing_on_disk",
            attachment_id=att_id,
            bytes_path=bytes_path,
        )
        raise AMCError(
            code=ErrorCode.MESSAGE_NOT_FOUND,
            message=f"Attachment {att_id!r} not found.",
            details={"attachment_id": att_id},
        )
    return bytes_path


def _extract_reply_to_for_discord(reply_to: str | None) -> str | None:
    """Translate the AMC ``msg_<ULID>`` reply-to to a Discord snowflake.

    The Discord connector's ``send(reply_to=...)`` expects a Discord message
    snowflake. AMC ``reply_to`` is the canonical AMC message id (``msg_<ULID>``),
    which would have to be looked up in ``messages.raw_json`` to recover the
    snowflake. That cross-platform reply-mapping is post-v1 (see
    ``amc/connectors/discord/connector.py:on_message`` -- ``reply_to=None``).
    For now we intentionally drop ``reply_to`` rather than crash; the message
    is still sent, just without a Discord ``message_reference``.
    """
    # ``reply_to`` is intentionally unused — see docstring. Accepting the
    # arg keeps the route signature stable so the lookup path can be wired
    # later without re-plumbing.
    _ = reply_to
    return None


def _map_discord_error(result: DiscordSendResult, channel_id: str) -> AMCError:
    """Translate a connector :class:`SendResult` failure to an :class:`AMCError`."""
    detail = {"channel_id": channel_id, "platform_detail": result.detail}
    if result.error == AmcDiscordErrorCode.PLATFORM_AUTH.value:
        return AMCError(
            code=ErrorCode.PLATFORM_AUTH,
            message="Discord rejected the bot credentials (401/403).",
            details=detail,
        )
    # Includes PLATFORM_SEND_FAILED and CHANNEL_NOT_FOUND from the connector.
    # Connector's CHANNEL_NOT_FOUND is rare (the channel row exists but the
    # discord client cache doesn't have it). We surface as PLATFORM_SEND_FAILED
    # to keep the route's CHANNEL_NOT_FOUND meaning (no row in our table)
    # unambiguous.
    return AMCError(
        code=ErrorCode.PLATFORM_SEND_FAILED,
        message=f"Discord send failed: {result.detail or 'unknown error'}",
        details=detail,
    )


async def _persist_outbound(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    message_id: str,
    source: Source,
    channel_id: str,
    channel_type: str,
    text_body: str,
    reply_to: str | None,
    message_ts: datetime,
    platform_message_id: str | None,
) -> None:
    """Insert an outbound ``messages`` row in its own short transaction.

    The agent itself is the sender for outbound rows; we UPSERT a synthetic
    ``agent`` sender row per source so the FK from ``messages.sender_id``
    resolves. The schema CHECK on ``senders.allowlist_status`` only allows
    ``allowed`` / ``unknown`` so we mark the agent as ``allowed`` (it is
    "us"). The platform-native message id is stashed in ``raw_json`` so a
    future retry / cross-reference can recover it.
    """
    msg_ts_str = _format_ts(message_ts)
    raw_json: str | None = None
    if platform_message_id is not None:
        raw_json = json.dumps(
            {"platform_message_id": platform_message_id},
            separators=(",", ":"),
        )

    agent_sender_id = "agent"
    async with session_factory() as session, session.begin():
        # Synthetic agent sender — UPSERT so concurrent sends don't race on
        # the first INSERT. ``allowlist_status='allowed'`` because the agent
        # is the principal of the system; the row is informational.
        await session.execute(
            text(
                "INSERT INTO senders "
                "(source, sender_id, display_name, allowlist_status, "
                " person_id, first_seen, last_seen) "
                "VALUES (:source, :sender_id, :display_name, 'allowed', "
                "        NULL, :now, :now) "
                "ON CONFLICT(source, sender_id) DO UPDATE SET "
                "  last_seen = excluded.last_seen"
            ),
            {
                "source": source.value,
                "sender_id": agent_sender_id,
                "display_name": "agent",
                "now": msg_ts_str,
            },
        )
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, source, channel_id, channel_type, sender_id, text, "
                " reply_to, direction, allowlist_status, message_ts, "
                " attachments_json, raw_json, delivery_status) "
                "VALUES (:id, :source, :channel_id, :channel_type, "
                "        :sender_id, :text, :reply_to, 'outbound', "
                "        'outbound', :message_ts, NULL, :raw_json, 'sent')"
            ),
            {
                "id": message_id,
                "source": source.value,
                "channel_id": channel_id,
                "channel_type": channel_type,
                "sender_id": agent_sender_id,
                "text": text_body,
                "reply_to": reply_to,
                "message_ts": msg_ts_str,
                "raw_json": raw_json,
            },
        )


def _utcnow() -> datetime:
    """Wall-clock UTC. Indirected so future tests can monkey-patch it."""
    return datetime.now(tz=UTC)


def _format_ts(dt: datetime) -> str:
    """Render a UTC datetime in millisecond-precision RFC 3339 with ``Z`` suffix."""
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:") + (
        f"{aware.second:02d}.{aware.microsecond // 1000:03d}Z"
    )
