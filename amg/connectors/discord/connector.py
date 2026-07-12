"""Discord platform connector (spec §5.1, §5.2, §7.5 Discord path).

Owns both halves of the Discord ↔ AMG bridge:

* **Inbound**: a ``discord.py`` :class:`discord.Client` runs as an asyncio
  task in the adapter process. ``MESSAGE_CREATE`` events fire ``on_message``,
  which (1) resolves the sender against the loaded
  :class:`amg.core.allowlist.AllowlistLoader`, (2) builds an
  :class:`amg.core.envelope.Envelope`, and (3) hands it to the injected
  :class:`amg.core.message_sink.MessageSink`. Cursor (``connector_state``)
  is advanced inside the same DB transaction as the message INSERT, per
  spec §5.1 Technical Notes.

* **Outbound**: :meth:`DiscordConnector.send` resolves the
  ``discord:dm:<id>`` / ``discord:channel:<id>`` channel id, calls
  ``channel.send(...)`` (with an optional :class:`discord.MessageReference`
  for ``reply_to``), and maps ``discord.py`` exceptions to AMG error
  codes:

  * ``discord.Forbidden`` (401/403) → :data:`AmgDiscordErrorCode.PLATFORM_AUTH`
  * any other ``discord.HTTPException`` → :data:`AmgDiscordErrorCode.PLATFORM_SEND_FAILED`

Channel id format
-----------------
Per spec §7.3.1: ``discord:dm:<id>`` for DM channels, ``discord:channel:<id>``
for guild text channels. The connector is the only place that constructs or
parses these strings; the adapter and MCP layers treat them as opaque.

Cursor format
-------------
``connector_state.cursor`` for ``source='discord'`` is JSON::

    {"seq": <int>, "resume_url": <string|null>}

``seq`` is the last gateway sequence number observed (``client.ws.sequence``
at INSERT time); ``resume_url`` is the resume gateway URL the gateway
returned in the most recent READY (``client.ws.gateway`` once attached, else
``None``). Both fields are read but never authoritatively consumed by the
connector — ``discord.py`` manages its own RESUME loop. They are persisted
so an external operator can inspect cursor progress and so a future
v2 reconnect-from-DB story can lift them without schema changes.

Testability hooks
-----------------
The connector exposes ``gateway_url`` and ``rest_base_url`` constructor
kwargs so test harnesses can point ``discord.py`` at the in-process fakes
(``tests/fakes/discord_gateway.py`` and ``tests/fakes/discord_rest.py``).
When ``gateway_url`` is set, :meth:`DiscordConnector.login` follows the
``_HarnessClient`` pattern: it bypasses ``HTTPClient.static_login`` (which
would call the live ``/users/@me`` endpoint), initializes the aiohttp
``ClientSession`` directly, and redirects
``DiscordWebSocket.DEFAULT_GATEWAY`` to the fake URL. In production
(``gateway_url=None``) the parent ``Client.login`` runs unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import aiohttp
import discord
import yarl
from discord.gateway import DiscordWebSocket
from discord.http import DiscordClientWebSocketResponse
from ulid import ULID

from amg.core.allowlist import AllowlistLoader
from amg.core.envelope import (
    ChannelType,
    Direction,
    Envelope,
    Sender,
    Source,
)
from amg.core.message_sink import MessageSink

__all__ = [
    "AmgDiscordErrorCode",
    "ChannelIdParseError",
    "DiscordConnector",
    "SendResult",
    "build_channel_id",
    "parse_channel_id",
]

_logger = logging.getLogger("amg.connectors.discord.connector")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class AmgDiscordErrorCode(StrEnum):
    """Error categories surfaced by :meth:`DiscordConnector.send`.

    These mirror the canonical :class:`amg.core.errors.ErrorCode` values that
    the HTTP send route translates into for the ``error.code`` envelope
    (spec §7.4.12). The connector returns them as plain strings on the
    :class:`SendResult` so the adapter route can pick the matching
    :class:`amg.core.errors.AMGError`.
    """

    PLATFORM_AUTH = "PLATFORM_AUTH"
    PLATFORM_SEND_FAILED = "PLATFORM_SEND_FAILED"
    CHANNEL_NOT_FOUND = "CHANNEL_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of an outbound :meth:`DiscordConnector.send` call.

    Attributes:
        ok: ``True`` on a successful send. ``False`` for any error.
        message_id: Platform-native Discord message id (snowflake as str)
            on success; ``None`` on failure.
        error: One of :class:`AmgDiscordErrorCode` values when ``ok`` is
            ``False``; ``None`` on success.
        detail: Human-readable detail (Discord error message) preserved for
            logs; ``None`` when there's nothing to record.
    """

    ok: bool
    message_id: str | None = None
    error: str | None = None
    detail: str | None = None


class ChannelIdParseError(ValueError):
    """Raised when a ``discord:...`` channel id can't be parsed."""


# ---------------------------------------------------------------------------
# Channel-id helpers
# ---------------------------------------------------------------------------


def build_channel_id(*, channel_type: ChannelType, discord_id: int | str) -> str:
    """Build the spec-§7.3.1 channel id for a Discord channel.

    Args:
        channel_type: ``ChannelType.DM`` → ``discord:dm:<id>``;
            ``ChannelType.GROUP`` → ``discord:channel:<id>``.
        discord_id: Numeric snowflake (or its str repr).

    Examples:
        >>> build_channel_id(channel_type=ChannelType.DM, discord_id=1234)
        'discord:dm:1234'
        >>> build_channel_id(channel_type=ChannelType.GROUP, discord_id=5678)
        'discord:channel:5678'
    """
    suffix = "dm" if channel_type is ChannelType.DM else "channel"
    return f"discord:{suffix}:{discord_id}"


def parse_channel_id(channel_id: str) -> tuple[ChannelType, int]:
    """Parse a ``discord:dm:<id>`` / ``discord:channel:<id>`` string.

    Returns the ``(ChannelType, snowflake_int)`` tuple.

    Raises:
        ChannelIdParseError: prefix is wrong, the kind is not
            ``dm``/``channel``, or the snowflake isn't numeric.
    """
    parts = channel_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "discord":
        raise ChannelIdParseError(
            f"Discord channel id must look like 'discord:dm:<id>' or "
            f"'discord:channel:<id>'; got {channel_id!r}."
        )
    kind = parts[1]
    if kind == "dm":
        ctype = ChannelType.DM
    elif kind == "channel":
        ctype = ChannelType.GROUP
    else:
        raise ChannelIdParseError(
            f"Discord channel id kind must be 'dm' or 'channel'; got {kind!r}."
        )
    try:
        snowflake = int(parts[2])
    except ValueError as exc:
        raise ChannelIdParseError(
            f"Discord channel snowflake must be numeric; got {parts[2]!r}."
        ) from exc
    return ctype, snowflake


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------


def _make_intents() -> discord.Intents:
    """Minimum intents needed for inbound DMs + guild messages with content.

    ``MESSAGE_CONTENT`` is a privileged intent and must be enabled in the
    Discord Developer Portal in addition to being requested here (spec §7.5
    Discord path: "Message Content intent enabled").
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    intents.dm_messages = True
    return intents


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class DiscordConnector(discord.Client):
    """Discord connector — single ``discord.Client`` driving the AMG bridge.

    Construct with the bot token, optional fake gateway / REST URLs (tests),
    a :class:`MessageSink` and an :class:`AllowlistLoader`. Run by calling
    ``await connector.start(bot_token)`` exactly like a normal
    ``discord.py`` bot — which is what the adapter does inside its asyncio
    task.

    Args:
        bot_token: Bot token. In production this is read from
            ``$AMG_DISCORD_BOT_TOKEN`` and passed in by the adapter; tests
            pass a placeholder ("fake-token-not-used") because login is
            short-circuited when ``gateway_url`` is set.
        message_sink: Persistence chokepoint. Inbound envelopes are handed
            to ``message_sink.record_inbound(envelope, source=cursor_json)``.
        allowlist: Loaded :class:`AllowlistLoader`. Used to decide
            ``allowed`` vs ``unknown`` ``allowlist_status`` per inbound
            message; also used to override the platform display name when
            the entry sets one.
        gateway_url: Optional ``ws://...`` URL for the fake gateway. When
            set, :meth:`login` follows the ``_HarnessClient`` pattern
            (bypass HTTP login, redirect ``DiscordWebSocket.DEFAULT_GATEWAY``
            to this URL). When ``None``, the real Discord login runs
            unchanged.
        rest_base_url: Optional REST base URL override. Currently unused —
            tests patch ``discord.http.HTTPClient.request`` directly via
            :class:`tests.fakes.discord_rest.FakeDiscordRest`. Reserved for
            a future ``aiohttp``-level interception layer.
        allowed_guild_channels: Optional iterable of guild-channel snowflakes
            (ints or numeric strings) the connector is configured to
            process. When ``None`` (the default) every guild-channel
            message reaches the sink. When set, guild-channel messages
            whose ``channel.id`` is not in the set are ignored (DMs are
            unaffected). This implements spec §3.1 ("Discord DMs and
            *configured* server-channel messages").

    Notes on threading:
        ``discord.py`` runs its event loop tasks on a single asyncio loop;
        every ``on_message`` invocation is on that loop. The
        :class:`MessageSink` is async-safe (each call opens its own short
        DB transaction), so concurrent inbound + outbound is serialised at
        SQLite's WAL writer lock — not in this class.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        message_sink: MessageSink,
        allowlist: AllowlistLoader,
        gateway_url: str | None = None,
        rest_base_url: str | None = None,
        allowed_guild_channels: Iterable[int | str] | None = None,
        intents: discord.Intents | None = None,
    ) -> None:
        super().__init__(intents=intents or _make_intents())
        self._bot_token = bot_token
        self._sink = message_sink
        self._allowlist = allowlist
        self._fake_gateway_url = gateway_url
        self._rest_base_url = rest_base_url
        self._allowed_guild_channels: frozenset[int] | None = (
            frozenset(int(c) for c in allowed_guild_channels)
            if allowed_guild_channels is not None
            else None
        )
        # Public marker the adapter reads when the bot token is rejected
        # (spec §5.1 error handling: "Connector logs ERROR, marks itself
        # degraded in /healthz"). Set by on_error / login failure paths.
        self.degraded: bool = False

    # ------------------------------------------------------------------
    # Lifecycle: login bypass for tests
    # ------------------------------------------------------------------

    async def login(self, token: str) -> None:
        """Either bypass login (test harness) or defer to ``Client.login``.

        When ``gateway_url`` is set the connector cannot reach
        ``discordapp.com``; we replicate the minimum that
        ``HTTPClient.static_login`` would have done (init the aiohttp
        ``ClientSession``, set the global rate-limit gate, stash the
        token) and then point ``DiscordWebSocket.DEFAULT_GATEWAY`` at the
        fake.

        When ``gateway_url`` is ``None`` the parent does the real OAuth
        round-trip.
        """
        if self._fake_gateway_url is None:
            await super().login(token)
            return

        # ---- test-only login bypass (mirrors tests/fakes/test_discord_gateway.py) ----
        await self._async_setup_hook()

        http = self.http
        if http.connector is discord.utils.MISSING:
            http.connector = aiohttp.TCPConnector(limit=0)

        # Mangled name: HTTPClient.__session -> _HTTPClient__session.
        http._HTTPClient__session = aiohttp.ClientSession(
            connector=http.connector,
            ws_response_class=DiscordClientWebSocketResponse,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        http._global_over = asyncio.Event()
        http._global_over.set()
        http.token = token

        # parse_ready will replace these with the fake's READY payload.
        self._connection.user = None
        self._connection.application_id = 1_000_000_000_000_000_002

        DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(self._fake_gateway_url)

    # ------------------------------------------------------------------
    # Inbound: discord.py event hook
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        """Route every inbound ``MESSAGE_CREATE`` through the sink.

        Skipped:
        * Messages authored by this bot (``author.id == self.user.id``) —
          the bot's own outbound is recorded by :meth:`send`.
        * Guild-channel messages whose channel id is not in
          ``allowed_guild_channels`` (when configured). DMs are always
          processed.

        On success the envelope is persisted and the cursor advances. On
        any unexpected error the exception is logged but not re-raised so
        ``discord.py``'s dispatcher continues running.
        """
        try:
            if self.user is not None and message.author.id == self.user.id:
                return

            channel_type = self._classify_channel(message)
            if channel_type is None:
                # Guild-channel message that wasn't on the allow list.
                return

            envelope = self._build_envelope(message, channel_type)
            cursor = self._snapshot_cursor()
            await self._sink.record_inbound(envelope, source=cursor)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "discord_inbound_record_failed",
                extra={"discord_message_id": getattr(message, "id", None)},
            )

    # ------------------------------------------------------------------
    # Inbound helpers
    # ------------------------------------------------------------------

    def _classify_channel(self, message: discord.Message) -> ChannelType | None:
        """Return DM/GROUP or ``None`` to drop a guild message."""
        if isinstance(message.channel, discord.DMChannel) or message.guild is None:
            return ChannelType.DM
        if (
            self._allowed_guild_channels is not None
            and int(message.channel.id) not in self._allowed_guild_channels
        ):
            return None
        return ChannelType.GROUP

    def _build_envelope(
        self,
        message: discord.Message,
        channel_type: ChannelType,
    ) -> Envelope:
        """Translate a ``discord.Message`` into a normalized envelope.

        Allowlist resolution happens twice: once here so the platform
        ``display_name`` can be overridden by the allowlist entry's
        configured value, once in :class:`MessageSink` for the final
        ``allowlist_status`` snapshot. Both calls are cheap dict lookups
        against the in-memory loader.
        """
        sender_id = str(message.author.id)
        entry = self._allowlist.resolve(Source.DISCORD, sender_id)
        display_name = (
            entry.display_name
            if entry is not None and entry.display_name
            else (
                getattr(message.author, "global_name", None)
                or getattr(message.author, "name", None)
                or sender_id
            )
        )

        sender = Sender(
            id=sender_id,
            display_name=display_name,
            person_id=entry.person_id if entry is not None else None,
        )

        channel_id = build_channel_id(
            channel_type=channel_type,
            discord_id=message.channel.id,
        )

        timestamp = message.created_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        reply_to = None  # cross-platform reply mapping is post-v1; raw carries it.

        raw: dict[str, Any] = {
            "discord_message_id": str(message.id),
            "discord_channel_id": str(message.channel.id),
            "discord_author_id": sender_id,
            "discord_guild_id": (
                str(message.guild.id) if message.guild is not None else None
            ),
            "discord_message_reference_id": (
                str(message.reference.message_id)
                if message.reference is not None and message.reference.message_id
                else None
            ),
        }

        return Envelope(
            id=f"msg_{ULID()}",
            source=Source.DISCORD,
            channel_id=channel_id,
            channel_type=channel_type,
            sender=sender,
            text=message.content or "",
            attachments=[],  # task #49 will re-host inbound attachments
            reply_to=reply_to,
            timestamp=timestamp,
            direction=Direction.INBOUND,
            raw=raw,
        )

    def _snapshot_cursor(self) -> str:
        """JSON-encode the current gateway sequence + resume URL.

        Returns ``{"seq": <int|null>, "resume_url": <str|null>}``. ``seq``
        is ``client.ws.sequence`` and ``resume_url`` is the resume URL the
        gateway sent in READY. When the connector hasn't connected yet
        (e.g. unit-test bench) both fields are ``null``.
        """
        ws = self.ws
        seq = getattr(ws, "sequence", None) if ws is not None else None
        resume_url = getattr(ws, "_gateway", None) if ws is not None else None
        if isinstance(resume_url, yarl.URL):
            resume_url = str(resume_url)
        return json.dumps(
            {"seq": seq, "resume_url": resume_url},
            separators=(",", ":"),
        )

    # ------------------------------------------------------------------
    # Outbound: send
    # ------------------------------------------------------------------

    async def send(
        self,
        channel_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        attachments: Sequence[Any] | None = None,
    ) -> SendResult:
        """Send a message to a Discord channel and return the platform id.

        Args:
            channel_id: AMG-shaped channel id (``discord:dm:<id>`` or
                ``discord:channel:<id>``).
            text: Message text. Empty allowed if ``attachments`` is set.
            reply_to: Discord message snowflake (or numeric str) the new
                message should reference. Translated to a
                :class:`discord.MessageReference` with
                ``fail_if_not_exists=False`` so a deleted target doesn't
                blow up the send.
            attachments: Optional list of :class:`discord.File` instances.
                ``discord.py`` 5xx retry rewinds the file via
                ``f.reset(seek=tries)`` so each attachment must be a
                seekable buffer.

        Returns:
            :class:`SendResult` — ``ok=True`` with ``message_id`` (str)
            on success; ``ok=False`` with ``error=PLATFORM_AUTH`` for
            401/403 and ``error=PLATFORM_SEND_FAILED`` for any other
            ``HTTPException``.

        Notes:
            ``discord.py`` retries 5xx (``500/502/504/524``) and 429
            internally; only the *final* outcome reaches us. A persistent
            server failure surfaces as ``DiscordServerError`` (subclass
            of ``HTTPException``) → ``PLATFORM_SEND_FAILED``.
        """
        try:
            ctype, snowflake = parse_channel_id(channel_id)
        except ChannelIdParseError as exc:
            return SendResult(
                ok=False,
                error=AmgDiscordErrorCode.PLATFORM_SEND_FAILED.value,
                detail=str(exc),
            )

        channel = self._resolve_channel(ctype, snowflake)
        if channel is None:
            return SendResult(
                ok=False,
                error=AmgDiscordErrorCode.CHANNEL_NOT_FOUND.value,
                detail=f"Discord channel {snowflake} not found in client cache.",
            )

        ref: discord.MessageReference | None = None
        if reply_to is not None:
            try:
                ref = discord.MessageReference(
                    message_id=int(reply_to),
                    channel_id=snowflake,
                    fail_if_not_exists=False,
                )
            except (TypeError, ValueError) as exc:
                return SendResult(
                    ok=False,
                    error=AmgDiscordErrorCode.PLATFORM_SEND_FAILED.value,
                    detail=f"Invalid reply_to {reply_to!r}: {exc}",
                )

        send_kwargs: dict[str, Any] = {}
        if ref is not None:
            send_kwargs["reference"] = ref
        if attachments:
            attachments = list(attachments)
            if len(attachments) == 1:
                send_kwargs["file"] = attachments[0]
            else:
                send_kwargs["files"] = attachments

        try:
            sent = await channel.send(text, **send_kwargs)
        except discord.Forbidden as exc:
            self.degraded = True
            _logger.error(
                "discord_send_forbidden",
                extra={"channel_id": channel_id, "status": exc.status},
            )
            return SendResult(
                ok=False,
                error=AmgDiscordErrorCode.PLATFORM_AUTH.value,
                detail=str(exc),
            )
        except discord.HTTPException as exc:
            # 401 lands here too (NotFound is also a subclass; the bot-auth
            # case is mostly 401/403 — we already caught Forbidden above).
            if exc.status == 401:
                self.degraded = True
                code: str = AmgDiscordErrorCode.PLATFORM_AUTH.value
            else:
                code = AmgDiscordErrorCode.PLATFORM_SEND_FAILED.value
            _logger.error(
                "discord_send_failed",
                extra={
                    "channel_id": channel_id,
                    "status": exc.status,
                    "code": code,
                },
            )
            return SendResult(ok=False, error=code, detail=str(exc))

        return SendResult(ok=True, message_id=str(sent.id))

    # ------------------------------------------------------------------
    # Outbound helpers
    # ------------------------------------------------------------------

    def _resolve_channel(
        self,
        channel_type: ChannelType,
        snowflake: int,
    ) -> discord.abc.Messageable | None:
        """Return a sendable channel object for ``snowflake``.

        Strategy:
          1. ``client.get_channel(snowflake)`` for guild channels and
             DM channels already in cache.
          2. Fall back to a :class:`discord.PartialMessageable` so tests
             (and cold-cache production paths) can still call ``.send``
             without a full guild fetch.

        For DMs the fallback :class:`PartialMessageable` is constructed
        with ``type=ChannelType.private``; for guild channels it uses
        ``ChannelType.text`` (the discord.py default). Either type lets
        ``channel.send`` route through ``HTTPClient.send_message`` to
        ``POST /channels/{snowflake}/messages``, which is the only REST
        contract the adapter cares about.
        """
        if channel_type is ChannelType.DM:
            return discord.PartialMessageable(
                state=self._connection,
                id=snowflake,
                type=discord.ChannelType.private,
            )
        cached = self.get_channel(snowflake)
        if cached is not None and isinstance(cached, discord.abc.Messageable):
            return cached
        return discord.PartialMessageable(
            state=self._connection,
            id=snowflake,
            type=discord.ChannelType.text,
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        """Log unhandled dispatch-handler exceptions.

        The default ``Client.on_error`` writes to stderr; we surface the
        same info via the stdlib logger so ``amg.core.logging``'s
        structlog bridge captures it as a structured event.
        """
        _logger.exception(
            "discord_on_error",
            extra={"event_method": event_method},
        )


# ---------------------------------------------------------------------------
# Convenience: standalone cursor parser (debug / health endpoint helper)
# ---------------------------------------------------------------------------


def parse_cursor(cursor_json: str) -> tuple[int | None, str | None]:
    """Parse a stored ``connector_state.cursor`` for ``source='discord'``.

    Returns ``(seq, resume_url)``. Empty string / malformed JSON returns
    ``(None, None)`` so a fresh-install cursor is observable as "no
    progress yet".
    """
    if not cursor_json:
        return None, None
    try:
        decoded = json.loads(cursor_json)
    except json.JSONDecodeError:
        return None, None
    seq = decoded.get("seq")
    resume_url = decoded.get("resume_url")
    if seq is not None and not isinstance(seq, int):
        seq = None
    if resume_url is not None and not isinstance(resume_url, str):
        resume_url = None
    return seq, resume_url


# Silence "imported but unused" lint warnings for symbols re-exported via
# ``__all__`` but only used inside the module body (datetime / Iterable).
_ = (datetime,)
