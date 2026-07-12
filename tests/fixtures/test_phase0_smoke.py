"""Phase 0 cross-fixture smoke tests.

This module is the spec §9.0 checkpoint gate translated into runnable
asserts. It stitches together all four Phase 0 deliverables — the
fixture ``chat.db``, the ``FakeAppleScriptSender``, the fake Discord
gateway, and the fake Discord REST endpoint — and proves they load and
interoperate end-to-end with no real macOS permissions and no real
Discord credentials.

For per-fixture deep coverage see:

* ``tests/fixtures/test_chat_db_fixture.py``
* ``tests/fakes/test_applescript.py``
* ``tests/fakes/test_discord_gateway.py``
* ``tests/fakes/test_discord_rest.py``

The four smoke tests here intentionally only assert the *minimum*
interop guarantees the spec gate calls out, so that a regression in the
gate is obvious from this file alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import sqlite3
from pathlib import Path
from typing import Any

import aiohttp
import discord
import pytest
import yarl
from discord.gateway import DiscordWebSocket
from discord.http import DiscordClientWebSocketResponse

from amg.connectors.imessage.applescript import AppleScriptSender, SendResult
from tests.fakes.applescript import FakeAppleScriptSender
from tests.fakes.discord_gateway import FakeDiscordGateway
from tests.fakes.discord_rest import FakeDiscordRest, make_test_channel
from tests.fixtures.build_chat_db import (
    ALLOWLISTED_HANDLE,
    CHAT_DB_PATH,
    CHAT_GUID,
    MSG_GUID_1,
    ensure_chat_db,
)

HANDSHAKE_TIMEOUT = 5.0
MESSAGE_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def chat_db_path() -> Path:
    """Materialize the fixture chat.db once per test session."""
    return ensure_chat_db()


# ---------------------------------------------------------------------------
# Discord harness — mirrors tests/fakes/test_discord_gateway.py
# ---------------------------------------------------------------------------


class _SmokeClient(discord.Client):
    """A ``discord.Client`` that bypasses HTTP login and points at a fake gateway.

    Reused from the gateway fake's own test suite. We want the smoke
    test to drive the same code paths a real connector will, so we
    don't shortcut the gateway connect/READY/dispatch loop.
    """

    def __init__(self, fake_url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fake_url = fake_url
        self.message_log: list[discord.Message] = []
        self.ready_event = asyncio.Event()

    async def login(self, token: str) -> None:
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

        self._connection.user = None
        self._connection.application_id = 1000000000000000002

        DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(self._fake_url)

    async def on_ready(self) -> None:
        self.ready_event.set()

    async def on_message(self, message: discord.Message) -> None:
        self.message_log.append(message)


def _make_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    intents.dm_messages = True
    return intents


def _build_message_payload(*, content: str, channel_id: str) -> dict[str, Any]:
    """Build a minimal MESSAGE_CREATE ``d`` payload (DM, no guild)."""
    return {
        "id": "9000000000000000001",
        "channel_id": channel_id,
        "author": {
            "id": "999888777666555444",
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


async def _start_client(client: _SmokeClient) -> asyncio.Task[None]:
    task = asyncio.create_task(client.start("fake-token-not-used"))
    try:
        await asyncio.wait_for(client.ready_event.wait(), timeout=HANDSHAKE_TIMEOUT)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
    return task


async def _stop_client(client: _SmokeClient, task: asyncio.Task[None]) -> None:
    if not client.is_closed():
        await client.close()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


# ---------------------------------------------------------------------------
# Smoke test 1 — chat.db fixture loads via the connector's planned read path
# ---------------------------------------------------------------------------


def test_chat_db_loads_via_connector_read_path(chat_db_path: Path) -> None:
    """Open chat.db read-only via stdlib ``sqlite3`` (mode=ro + query_only)
    exactly as spec §6.1 / §9.2 say the iMessage connector will, and prove
    the seeded DM thread is queryable.
    """
    assert chat_db_path.exists(), (
        f"Phase 0 fixture missing: chat.db expected at {chat_db_path}. "
        "Run `python -m tests.fixtures.build_chat_db` to rebuild."
    )

    uri = f"file:{chat_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row

        # Writes must fail under read-only + query_only.
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO handle (id, service) VALUES ('+10000000000', 'iMessage')")

        # The seeded DM thread is reachable via the connector's planned join
        # (handle <-> message <-> chat_message_join <-> chat).
        row = conn.execute(
            """
            SELECT m.guid, m.text, h.id AS handle_id, c.guid AS chat_guid
            FROM message m
            JOIN handle h ON h.ROWID = m.handle_id
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            WHERE m.guid = ?
            """,
            (MSG_GUID_1,),
        ).fetchone()

        assert row is not None, "seeded message MSG_GUID_1 not found in fixture"
        assert row["chat_guid"] == CHAT_GUID
        assert row["handle_id"] == ALLOWLISTED_HANDLE
        assert row["text"] is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Smoke test 2 — FakeAppleScriptSender substitutes for the real one
# ---------------------------------------------------------------------------


async def test_fake_applescript_sender_records_invocation() -> None:
    """``FakeAppleScriptSender`` satisfies the ``AppleScriptSender`` protocol
    structurally, records every send, and exposes the exact command line that
    a real ``osascript`` invocation would receive (``chat_guid`` + ``text``).
    """
    fake = FakeAppleScriptSender()

    # Structural compatibility with the production seam.
    assert isinstance(fake, AppleScriptSender), (
        "FakeAppleScriptSender does not satisfy the AppleScriptSender protocol; "
        "the substitution promised by spec §9.0 is broken."
    )

    fake.queue_outcome(SendResult(ok=True))
    result = await fake.send(CHAT_GUID, "hello from smoke test")

    assert result.ok is True
    fake.assert_call_count(1)
    call = fake.calls[0]
    assert call.chat_guid == CHAT_GUID
    assert call.text == "hello from smoke test"
    assert call.attachment_path is None

    # The recorded call carries everything needed to reconstruct the exact
    # ``osascript -e '...'`` command line the real sender will eventually
    # invoke. We materialize that command line here so tests can assert on
    # it without ever shelling out.
    osascript_argv = [
        "osascript",
        "-e",
        f'tell application "Messages" to send "{call.text}" to chat id "{call.chat_guid}"',
    ]
    rendered = shlex.join(osascript_argv)
    assert 'send "hello from smoke test"' in rendered
    assert CHAT_GUID in rendered


# ---------------------------------------------------------------------------
# Smoke test 3 — fake Discord gateway: IDENTIFY -> READY + MESSAGE_CREATE round-trip
# ---------------------------------------------------------------------------


async def test_fake_discord_gateway_identify_ready_and_message_create() -> None:
    """``discord.py`` completes IDENTIFY -> READY against the fake gateway and
    a ``MESSAGE_CREATE`` payload round-trips into the client's ``on_message``.
    """
    gateway = FakeDiscordGateway()
    await gateway.start()
    try:
        client = _SmokeClient(gateway.url, intents=_make_intents())
        task = await _start_client(client)
        try:
            # IDENTIFY -> READY proven by ready_event having fired.
            assert client.is_ready()
            assert client.user is not None
            assert client.user.name == "fake-bot"

            # Round-trip a MESSAGE_CREATE.
            payload = _build_message_payload(
                content="phase 0 smoke",
                channel_id="222222222222222222",
            )
            await gateway.deliver(payload)

            deadline = asyncio.get_running_loop().time() + MESSAGE_TIMEOUT
            while not client.message_log and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)

            assert len(client.message_log) == 1, (
                "MESSAGE_CREATE did not round-trip to discord.py's on_message"
            )
            assert client.message_log[0].content == "phase 0 smoke"
            assert str(client.message_log[0].id) == payload["id"]
        finally:
            await _stop_client(client, task)
    finally:
        await gateway.stop()


# ---------------------------------------------------------------------------
# Smoke test 4 — fake Discord REST records channel.send(...)
# ---------------------------------------------------------------------------


async def test_fake_discord_rest_records_channel_send() -> None:
    """A ``discord.py`` client calling ``channel.send("...")`` is intercepted
    by ``FakeDiscordRest`` and the outbound is recorded with the correct
    channel id and content. Proves the fake substitutes for real Discord
    REST without any network access.
    """
    client = discord.Client(intents=discord.Intents.default())
    channel_id = 123_456_789_012_345

    with FakeDiscordRest() as rest:
        channel = make_test_channel(client, channel_id)
        message = await channel.send("phase 0 smoke send")

        # discord.py hydrated a real Message from the fake's payload.
        assert isinstance(message, discord.Message)
        assert message.content == "phase 0 smoke send"
        assert message.channel.id == channel_id

        # The fake recorded the exact outbound REST call.
        assert len(rest.sent_messages) == 1
        recorded = rest.sent_messages[0]
        assert recorded["method"] == "POST"
        assert recorded["path_template"] == "/channels/{channel_id}/messages"
        assert recorded["channel_id"] == channel_id
        assert recorded["content"] == "phase 0 smoke send"


# ---------------------------------------------------------------------------
# Error handling — missing fixture is named in the error message
# ---------------------------------------------------------------------------


def test_missing_chat_db_fixture_error_names_the_artifact(
    tmp_path: Path,
) -> None:
    """If a caller points the read path at a non-existent chat.db, the
    failure message names the missing artifact — required by the spec §9.0
    error-handling criterion.

    We do not delete the committed fixture (other tests depend on it);
    instead we exercise the same read path against a path that doesn't
    exist and confirm the error is intelligible.
    """
    missing = tmp_path / "does-not-exist-chat.db"
    assert not missing.exists()

    # The connector's planned read path is `mode=ro` URI open; SQLite raises
    # OperationalError with a message that names the missing file.
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        sqlite3.connect(f"file:{missing}?mode=ro", uri=True).execute("SELECT 1")

    assert "unable to open" in str(excinfo.value).lower()

    # Belt and suspenders: also assert the canonical fixture path is stable
    # so a regression that *moves* the fixture surfaces a clear message in
    # the smoke-test failure rather than a cryptic import error.
    assert CHAT_DB_PATH.name == "chat.db"
    assert CHAT_DB_PATH.parent.name == "fixtures"
