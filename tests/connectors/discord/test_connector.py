"""Tests for ``amg.connectors.discord.connector.DiscordConnector``.

Each test wires the connector to the in-process fakes built in tasks
#17/#18:

* :class:`tests.fakes.discord_gateway.FakeDiscordGateway` — accepts
  ``IDENTIFY``, dispatches ``READY`` and ``MESSAGE_CREATE``, supports a
  forced disconnect that triggers RESUME.
* :class:`tests.fakes.discord_rest.FakeDiscordRest` — patches
  ``discord.http.HTTPClient.request`` so ``channel.send`` round-trips
  without hitting the real Discord REST API.

A small ``_FakeMessageSink`` records every ``record_inbound`` call so the
tests can assert envelope shape, ``allowlist_status`` snapshot, and
cursor JSON without booting a real SQLite database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import Iterator
from typing import Any

import discord
import pytest

from amg.connectors.discord.connector import (
    AmgDiscordErrorCode,
    DiscordConnector,
    SendResult,
    build_channel_id,
    parse_channel_id,
    parse_cursor,
)
from amg.core.allowlist import AllowlistEntry, AllowlistLoader
from amg.core.envelope import (
    AllowlistStatus,
    ChannelType,
    Direction,
    Envelope,
    Source,
)
from tests.fakes.discord_gateway import FakeDiscordGateway
from tests.fakes.discord_rest import FakeDiscordRest

HANDSHAKE_TIMEOUT = 5.0
MESSAGE_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMessageSink:
    """Minimal stand-in for :class:`amg.core.message_sink.MessageSink`.

    Records every inbound call as ``(envelope, cursor, allowlist_status)``.
    The status is computed exactly like the real sink (resolve against the
    loader; ``allowed`` if present, else ``unknown``) so tests can assert
    the snapshot the connector would have caused without booting SQLite.
    """

    def __init__(self, allowlist: AllowlistLoader) -> None:
        self._allowlist = allowlist
        self.records: list[tuple[Envelope, str, AllowlistStatus]] = []

    async def record_inbound(self, envelope: Envelope, source: str) -> str:
        entry = self._allowlist.resolve(envelope.source, envelope.sender.id)
        status = AllowlistStatus.ALLOWED if entry is not None else AllowlistStatus.UNKNOWN
        self.records.append((envelope, source, status))
        return envelope.id


def _build_allowlist(entries: list[AllowlistEntry]) -> AllowlistLoader:
    """An in-memory :class:`AllowlistLoader` skipping the TOML parse."""
    loader = AllowlistLoader.__new__(AllowlistLoader)
    # Bypass the constructor's expanduser / Path() machinery — we never read
    # from disk in these tests.
    loader.path = None  # type: ignore[assignment]
    loader._entries = {(e.source, e.id): e for e in entries}
    return loader


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


ALLOWED_USER_ID = "999888777666555444"
QUARANTINED_USER_ID = "111222333444555666"
GUILD_ID = "200000000000000001"
ALLOWED_GUILD_CHANNEL_ID = "300000000000000001"
DENIED_GUILD_CHANNEL_ID = "300000000000000002"


def _dm_message_payload(
    *,
    content: str,
    author_id: str,
    channel_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Build a MESSAGE_CREATE payload without a guild_id (= DM)."""
    return {
        "id": message_id or str(secrets.randbits(63)),
        "channel_id": channel_id or str(secrets.randbits(63)),
        "author": {
            "id": author_id,
            "username": "alice",
            "discriminator": "0",
            "global_name": "Alice",
            "avatar": None,
            "bot": False,
        },
        "content": content,
        "timestamp": "2026-05-03T00:00:00.000000+00:00",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "type": 0,
        "pinned": False,
        "flags": 0,
    }


def _guild_message_payload(
    *,
    content: str,
    author_id: str,
    channel_id: str,
    guild_id: str = GUILD_ID,
) -> dict[str, Any]:
    """Build a MESSAGE_CREATE payload with a guild_id (= guild text channel)."""
    payload = _dm_message_payload(
        content=content,
        author_id=author_id,
        channel_id=channel_id,
    )
    payload["guild_id"] = guild_id
    return payload


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


async def _wait_ready(connector: DiscordConnector) -> None:
    deadline = asyncio.get_running_loop().time() + HANDSHAKE_TIMEOUT
    while not connector.is_ready() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    if not connector.is_ready():
        raise TimeoutError("connector did not become ready in time")


async def _start_connector(connector: DiscordConnector) -> asyncio.Task[None]:
    task = asyncio.create_task(connector.start("fake-token-not-used"))
    try:
        await asyncio.wait_for(_wait_ready(connector), timeout=HANDSHAKE_TIMEOUT)
    except (TimeoutError, Exception):
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
    return task


async def _stop_connector(connector: DiscordConnector, task: asyncio.Task[None]) -> None:
    if not connector.is_closed():
        await connector.close()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


async def _wait_for_records(sink: _FakeMessageSink, count: int) -> None:
    deadline = asyncio.get_running_loop().time() + MESSAGE_TIMEOUT
    while (
        len(sink.records) < count and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.02)
    if len(sink.records) < count:
        raise TimeoutError(
            f"expected {count} sink records; observed {len(sink.records)}"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def gateway() -> Any:
    g = FakeDiscordGateway()
    await g.start()
    try:
        yield g
    finally:
        await g.stop()


@pytest.fixture
def allowlist() -> AllowlistLoader:
    return _build_allowlist(
        [
            AllowlistEntry(
                source=Source.DISCORD,
                id=ALLOWED_USER_ID,
                display_name="Allowlisted Alice",
                person_id="person_alice",
            ),
        ]
    )


@pytest.fixture
def sink(allowlist: AllowlistLoader) -> _FakeMessageSink:
    return _FakeMessageSink(allowlist)


@pytest.fixture
def make_connector(
    gateway: FakeDiscordGateway,
    sink: _FakeMessageSink,
    allowlist: AllowlistLoader,
) -> Iterator[Any]:
    """Factory that builds connectors pinned to the same gateway + sink."""
    built: list[DiscordConnector] = []

    def _factory(
        *,
        allowed_guild_channels: list[str] | None = None,
    ) -> DiscordConnector:
        connector = DiscordConnector(
            bot_token="fake-token-not-used",
            message_sink=sink,
            allowlist=allowlist,
            gateway_url=gateway.url,
            allowed_guild_channels=allowed_guild_channels,
        )
        built.append(connector)
        return connector

    yield _factory

    # Best-effort cleanup if a test forgot to close.
    for c in built:
        if not c.is_closed():
            with contextlib.suppress(Exception):
                asyncio.get_event_loop().run_until_complete(c.close())


# ---------------------------------------------------------------------------
# Functional acceptance: gateway + intent
# ---------------------------------------------------------------------------


async def test_connector_completes_identify_with_message_content_intent(
    gateway: FakeDiscordGateway,
    make_connector: Any,
) -> None:
    """The connector connects + READY-handshakes and asks for MESSAGE_CONTENT."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        assert connector.is_ready()
        # The intent enum must include MESSAGE_CONTENT (privileged intent
        # required to read message bodies — spec §7.5).
        assert connector.intents.message_content is True
        assert connector.intents.dm_messages is True
        assert connector.intents.messages is True
    finally:
        await _stop_connector(connector, task)


# ---------------------------------------------------------------------------
# Functional acceptance: inbound DM allowlist resolution
# ---------------------------------------------------------------------------


async def test_allowlisted_dm_yields_envelope_with_status_allowed(
    gateway: FakeDiscordGateway,
    make_connector: Any,
    sink: _FakeMessageSink,
) -> None:
    """An allowlisted DM produces an envelope; sink reports ``allowed``."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        await gateway.deliver(
            _dm_message_payload(
                content="hi from alice",
                author_id=ALLOWED_USER_ID,
                channel_id="400000000000000001",
            )
        )
        await _wait_for_records(sink, 1)

        envelope, cursor_json, status = sink.records[0]
        assert status is AllowlistStatus.ALLOWED
        assert envelope.source is Source.DISCORD
        assert envelope.channel_type is ChannelType.DM
        assert envelope.channel_id == "discord:dm:400000000000000001"
        assert envelope.sender.id == ALLOWED_USER_ID
        # Allowlist override wins over the platform-supplied global_name.
        assert envelope.sender.display_name == "Allowlisted Alice"
        assert envelope.sender.person_id == "person_alice"
        assert envelope.text == "hi from alice"
        assert envelope.direction is Direction.INBOUND

        # Cursor JSON is well-formed; seq is set after at least one dispatch.
        seq, resume_url = parse_cursor(cursor_json)
        assert isinstance(seq, int)
        assert seq >= 1
        # The fake's resume_gateway_url points back at itself.
        assert resume_url == gateway.url or resume_url is None
    finally:
        await _stop_connector(connector, task)


async def test_non_allowlisted_dm_yields_envelope_with_status_unknown(
    gateway: FakeDiscordGateway,
    make_connector: Any,
    sink: _FakeMessageSink,
) -> None:
    """A DM from an unknown sender still records — but tagged ``unknown``."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        await gateway.deliver(
            _dm_message_payload(
                content="hi from a stranger",
                author_id=QUARANTINED_USER_ID,
                channel_id="400000000000000002",
            )
        )
        await _wait_for_records(sink, 1)

        envelope, _cursor, status = sink.records[0]
        assert status is AllowlistStatus.UNKNOWN
        assert envelope.sender.id == QUARANTINED_USER_ID
        # No allowlist entry → display_name falls through to the platform value.
        assert envelope.sender.display_name == "Alice"
        assert envelope.sender.person_id is None
    finally:
        await _stop_connector(connector, task)


# ---------------------------------------------------------------------------
# Functional acceptance: guild-channel filtering
# ---------------------------------------------------------------------------


async def test_guild_message_in_allowed_channel_is_processed(
    gateway: FakeDiscordGateway,
    make_connector: Any,
    sink: _FakeMessageSink,
) -> None:
    """When ``allowed_guild_channels`` is set, allow-listed channels pass through."""
    connector = make_connector(allowed_guild_channels=[ALLOWED_GUILD_CHANNEL_ID])
    task = await _start_connector(connector)
    try:
        await gateway.deliver(
            _guild_message_payload(
                content="server hello",
                author_id=ALLOWED_USER_ID,
                channel_id=ALLOWED_GUILD_CHANNEL_ID,
            )
        )
        await _wait_for_records(sink, 1)

        envelope, _, status = sink.records[0]
        # Guild messages with a guild_id but no cached guild come through as
        # PartialMessageable / DMChannel depending on discord.py's hydration
        # path. The connector falls back to ChannelType.DM when there's no
        # resolvable guild (no guild fetch in tests). What we MUST guarantee
        # is that the channel id was preserved end-to-end and the message
        # was not silently dropped.
        assert envelope.channel_id.endswith(f":{ALLOWED_GUILD_CHANNEL_ID}")
        assert envelope.text == "server hello"
        assert status is AllowlistStatus.ALLOWED
    finally:
        await _stop_connector(connector, task)


async def test_guild_message_in_denied_channel_is_dropped(
    gateway: FakeDiscordGateway,
    make_connector: Any,
    sink: _FakeMessageSink,
) -> None:
    """When ``allowed_guild_channels`` is set, other channels are silently dropped."""
    connector = make_connector(allowed_guild_channels=[ALLOWED_GUILD_CHANNEL_ID])
    task = await _start_connector(connector)
    try:
        # Inject a payload whose channel resolves to a Guild channel via guild_id
        # but is NOT on the allow list. discord.py needs a guild for this to be
        # treated as guild-channel; without one it falls back to DM. We exercise
        # the direct connector hook to make the "denied guild channel" case
        # observable independent of hydration quirks.
        from unittest.mock import MagicMock

        denied_channel = MagicMock(spec=discord.TextChannel)
        denied_channel.id = int(DENIED_GUILD_CHANNEL_ID)
        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.id = int(ALLOWED_USER_ID)
        msg.guild = MagicMock()
        msg.guild.id = int(GUILD_ID)
        msg.channel = denied_channel
        msg.content = "should be dropped"
        msg.created_at = __import__("datetime").datetime(
            2026, 5, 3, tzinfo=__import__("datetime").UTC
        )
        msg.id = 12345
        msg.reference = None

        # Pretend we're not the bot author of this message.
        await connector.on_message(msg)

        # No record was delivered to the sink.
        assert sink.records == []
    finally:
        await _stop_connector(connector, task)


# ---------------------------------------------------------------------------
# Functional acceptance: bot-self loop guard
# ---------------------------------------------------------------------------


async def test_messages_authored_by_the_bot_are_skipped(
    gateway: FakeDiscordGateway,
    make_connector: Any,
    sink: _FakeMessageSink,
) -> None:
    """Echoes of our own bot's messages must not loop back through the sink."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        # The fake's READY uses bot user id 1000000000000000001.
        bot_id = str(connector.user.id) if connector.user is not None else "1000000000000000001"
        await gateway.deliver(
            _dm_message_payload(
                content="echo from bot",
                author_id=bot_id,
                channel_id="400000000000000003",
            )
        )

        # Give the dispatcher a moment to drop the event.
        await asyncio.sleep(0.15)
        assert sink.records == []
    finally:
        await _stop_connector(connector, task)


# ---------------------------------------------------------------------------
# Outbound: send returns platform message id
# ---------------------------------------------------------------------------


async def test_send_returns_platform_message_id(
    gateway: FakeDiscordGateway,
    make_connector: Any,
) -> None:
    """``send`` round-trips through the fake REST and returns the snowflake."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        with FakeDiscordRest() as rest:
            result = await connector.send(
                "discord:channel:500000000000000001",
                "outbound text",
            )

            assert result.ok is True
            assert result.message_id is not None
            assert result.message_id.isdigit()
            assert result.error is None

            # The fake recorded exactly one POST.
            assert len(rest.sent_messages) == 1
            recorded = rest.sent_messages[-1]
            assert recorded["channel_id"] == 500000000000000001
            assert recorded["content"] == "outbound text"
            assert recorded["method"] == "POST"
    finally:
        await _stop_connector(connector, task)


async def test_send_with_reply_to_uses_message_reference(
    gateway: FakeDiscordGateway,
    make_connector: Any,
) -> None:
    """``reply_to`` becomes a ``MessageReference`` in the REST request."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        with FakeDiscordRest() as rest:
            result = await connector.send(
                "discord:dm:600000000000000001",
                "reply!",
                reply_to="123456789",
            )
            assert result.ok is True

            recorded = rest.sent_messages[-1]
            assert recorded["message_reference"] is not None
            assert int(recorded["message_reference"]["message_id"]) == 123456789
    finally:
        await _stop_connector(connector, task)


# ---------------------------------------------------------------------------
# Outbound: error mapping
# ---------------------------------------------------------------------------


async def test_send_403_maps_to_platform_auth(
    gateway: FakeDiscordGateway,
    make_connector: Any,
) -> None:
    """Forbidden (403) → ``PLATFORM_AUTH`` and the connector goes degraded."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        with FakeDiscordRest() as rest:
            rest.script_response(
                "POST",
                "/channels/{channel_id}/messages",
                status=403,
                body={"message": "Missing access", "code": 50001},
            )
            result = await connector.send(
                "discord:channel:700000000000000001",
                "no permission",
            )
            assert result.ok is False
            assert result.error == AmgDiscordErrorCode.PLATFORM_AUTH.value
            assert connector.degraded is True
    finally:
        await _stop_connector(connector, task)


async def test_send_401_maps_to_platform_auth(
    gateway: FakeDiscordGateway,
    make_connector: Any,
) -> None:
    """A 401 (token rejected) is also surfaced as ``PLATFORM_AUTH``."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        with FakeDiscordRest() as rest:
            rest.script_response(
                "POST",
                "/channels/{channel_id}/messages",
                status=401,
                body={"message": "401: Unauthorized"},
            )
            result = await connector.send(
                "discord:channel:700000000000000002",
                "bad token",
            )
            assert result.ok is False
            assert result.error == AmgDiscordErrorCode.PLATFORM_AUTH.value
            assert connector.degraded is True
    finally:
        await _stop_connector(connector, task)


async def test_send_persistent_5xx_maps_to_platform_send_failed(
    gateway: FakeDiscordGateway,
    make_connector: Any,
) -> None:
    """A terminal (non-retried) 5xx surfaces as ``PLATFORM_SEND_FAILED``.

    We script a 503 (which is NOT in discord.py's retryable set so it goes
    terminal immediately) to keep the test fast and predictable.
    """
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        with FakeDiscordRest() as rest:
            rest.script_response(
                "POST",
                "/channels/{channel_id}/messages",
                status=503,
                body={"message": "service unavailable"},
            )
            result = await connector.send(
                "discord:channel:700000000000000003",
                "server is down",
            )
            assert result.ok is False
            assert result.error == AmgDiscordErrorCode.PLATFORM_SEND_FAILED.value
            # 5xx is NOT a credential failure — connector stays healthy so the
            # adapter doesn't shed all subsequent traffic on a transient outage.
            assert connector.degraded is False
    finally:
        await _stop_connector(connector, task)


async def test_send_500_then_success_recovers_via_discord_py_retry(
    gateway: FakeDiscordGateway,
    make_connector: Any,
) -> None:
    """A 500 + queued success exercises ``discord.py``'s built-in 5xx retry."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        with FakeDiscordRest() as rest:
            rest.script_server_error("POST", "/channels/{channel_id}/messages", status=500)
            result = await connector.send(
                "discord:channel:700000000000000004",
                "second time lucky",
            )
            assert result.ok is True
            statuses = [c["response_status"] for c in rest.sent_messages]
            assert statuses == [500, 200]
    finally:
        await _stop_connector(connector, task)


async def test_send_invalid_channel_id_returns_error(
    gateway: FakeDiscordGateway,
    make_connector: Any,
) -> None:
    """A malformed channel id is rejected before any REST call is made."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        with FakeDiscordRest() as rest:
            result = await connector.send("not-a-discord-channel", "x")
            assert result.ok is False
            assert result.error == AmgDiscordErrorCode.PLATFORM_SEND_FAILED.value
            assert rest.sent_messages == []
    finally:
        await _stop_connector(connector, task)


# ---------------------------------------------------------------------------
# Reconnect / cursor advancement
# ---------------------------------------------------------------------------


async def test_gateway_disconnect_does_not_duplicate_inserts(
    gateway: FakeDiscordGateway,
    make_connector: Any,
    sink: _FakeMessageSink,
) -> None:
    """A forced disconnect → discord.py RESUME → no duplicate envelopes."""
    connector = make_connector()

    # Hook ``on_resumed`` so we can wait for the RESUME handshake to complete
    # before delivering more messages. ``discord.Client.dispatch`` calls
    # ``on_resumed`` whenever the gateway emits a RESUMED dispatch.
    resumed = asyncio.Event()
    original_on_resumed = getattr(connector, "on_resumed", None)

    async def _on_resumed() -> None:
        if original_on_resumed is not None:
            await original_on_resumed()
        resumed.set()

    connector.on_resumed = _on_resumed  # type: ignore[method-assign]

    task = await _start_connector(connector)
    try:
        await gateway.deliver(
            _dm_message_payload(
                content="before disconnect",
                author_id=ALLOWED_USER_ID,
                channel_id="800000000000000001",
            )
        )
        await _wait_for_records(sink, 1)

        # Force a recoverable disconnect; discord.py reconnects + RESUMEs.
        await gateway.disconnect(reason="test-induced disconnect")

        # Wait for the RESUMED dispatch from the fake gateway.
        await asyncio.wait_for(resumed.wait(), timeout=HANDSHAKE_TIMEOUT)

        # Deliver another after resume; should arrive exactly once.
        await gateway.deliver(
            _dm_message_payload(
                content="after resume",
                author_id=ALLOWED_USER_ID,
                channel_id="800000000000000001",
            )
        )
        await _wait_for_records(sink, 2)

        contents = [env.text for env, _c, _s in sink.records]
        # The replayed pre-disconnect event must NOT appear a second time.
        assert contents == ["before disconnect", "after resume"]
    finally:
        await _stop_connector(connector, task)


async def test_cursor_advances_with_each_record(
    gateway: FakeDiscordGateway,
    make_connector: Any,
    sink: _FakeMessageSink,
) -> None:
    """``connector_state.cursor`` advances monotonically per delivered message."""
    connector = make_connector()
    task = await _start_connector(connector)
    try:
        # Stagger the deliveries so each on_message snapshot observes a
        # distinct ws.sequence value. discord.py updates the sequence as
        # each frame is received; without the await, a tight burst can
        # all see the same final seq because every dispatch is queued
        # before the first on_message coroutine runs.
        for i in range(3):
            await gateway.deliver(
                _dm_message_payload(
                    content=f"msg {i}",
                    author_id=ALLOWED_USER_ID,
                    channel_id="900000000000000001",
                )
            )
            await _wait_for_records(sink, i + 1)

        seqs = [parse_cursor(c)[0] for _e, c, _s in sink.records]
        # Every recorded cursor must carry a numeric seq, and the sequence
        # must be strictly monotonic — every fresh dispatch advances the
        # seq counter and is observed by the connector before the next
        # message lands.
        assert all(isinstance(s, int) for s in seqs), seqs
        assert seqs == sorted(seqs)
        assert seqs[-1] > seqs[0]
    finally:
        await _stop_connector(connector, task)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_channel_id_dm_and_guild() -> None:
    assert build_channel_id(channel_type=ChannelType.DM, discord_id=12) == "discord:dm:12"
    assert (
        build_channel_id(channel_type=ChannelType.GROUP, discord_id="34")
        == "discord:channel:34"
    )


def test_parse_channel_id_round_trip() -> None:
    assert parse_channel_id("discord:dm:99") == (ChannelType.DM, 99)
    assert parse_channel_id("discord:channel:1234567890") == (
        ChannelType.GROUP,
        1234567890,
    )


def test_parse_channel_id_rejects_garbage() -> None:
    from amg.connectors.discord.connector import ChannelIdParseError

    with pytest.raises(ChannelIdParseError):
        parse_channel_id("imessage:dm:1")
    with pytest.raises(ChannelIdParseError):
        parse_channel_id("discord:thread:1")
    with pytest.raises(ChannelIdParseError):
        parse_channel_id("discord:dm:not-a-number")
    with pytest.raises(ChannelIdParseError):
        parse_channel_id("malformed")


def test_parse_cursor_handles_empty_and_malformed() -> None:
    assert parse_cursor("") == (None, None)
    assert parse_cursor("not-json") == (None, None)
    assert parse_cursor(json.dumps({"seq": 7, "resume_url": "wss://gw"})) == (
        7,
        "wss://gw",
    )
    # Seq must be int; non-int falls through to None.
    assert parse_cursor(json.dumps({"seq": "x", "resume_url": "wss://gw"})) == (
        None,
        "wss://gw",
    )


def test_send_result_dataclass_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    r = SendResult(ok=True, message_id="123")
    with pytest.raises(FrozenInstanceError):
        r.ok = False  # type: ignore[misc]
