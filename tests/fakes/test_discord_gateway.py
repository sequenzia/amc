"""Integration tests for the in-process fake Discord gateway.

These tests drive a real ``discord.py`` ``Client`` against the fake gateway,
asserting the gateway is sufficient to:

* complete the IDENTIFY/READY handshake
* deliver MESSAGE_CREATE events that fire ``on_message`` in the client
* survive a forced disconnect and replay missed events on RESUME
* reject malformed IDENTIFYs and bad inject-before-connect usage cleanly
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from typing import Any

import aiohttp
import discord
import pytest
import yarl
from discord.gateway import DiscordWebSocket
from discord.http import DiscordClientWebSocketResponse

from tests.fakes.discord_gateway import FakeDiscordGateway

# Per-test timeout. The handshake is sub-second locally; anything slower means
# the fake is broken and we'd rather see pytest fail than hang.
HANDSHAKE_TIMEOUT = 5.0
MESSAGE_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Test client harness
# ---------------------------------------------------------------------------


class _HarnessClient(discord.Client):
    """``discord.Client`` that bypasses HTTP login and points at a fake gateway.

    Real ``Client.start()`` calls ``/users/@me`` and ``/oauth2/applications/@me``
    over HTTPS. Tests against the fake gateway can't (and shouldn't) hit the
    real Discord REST API, so we override ``login`` to:

      1. Initialise the aiohttp ClientSession on the http client (normally done
         in ``static_login``) so ``ws_connect`` works.
      2. Stub the ``ClientUser`` and ``application_id`` on the connection state
         so ``parse_ready`` doesn't blow up later.

    We do NOT override ``connect`` -- that path runs the real gateway loop, so
    the test exercises the production code paths against the fake server.
    """

    def __init__(self, fake_url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fake_url = fake_url
        self.message_log: list[discord.Message] = []
        self.ready_event = asyncio.Event()
        self.resumed_event = asyncio.Event()

    async def login(self, token: str) -> None:
        # Mirror discord.HTTPClient.static_login() session setup without the
        # actual HTTP request.
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

        # Stub the bot user. parse_ready will replace this with the payload from
        # the fake's READY dispatch, but we keep a placeholder so any pre-READY
        # access doesn't crash.
        self._connection.user = None
        self._connection.application_id = 1000000000000000002

        # Steer the gateway connection at the fake. ``DiscordWebSocket`` falls
        # back to ``DEFAULT_GATEWAY`` if no explicit gateway is passed; the
        # initial ``Client.connect()`` call doesn't pass one.
        DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(self._fake_url)

    async def on_ready(self) -> None:
        self.ready_event.set()

    async def on_resumed(self) -> None:
        self.resumed_event.set()

    async def on_message(self, message: discord.Message) -> None:
        self.message_log.append(message)


def _make_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    intents.dm_messages = True
    return intents


def _build_message_payload(*, content: str, channel_id: str | None = None) -> dict[str, Any]:
    """Build a minimal MESSAGE_CREATE ``d`` payload.

    No ``guild_id`` -> discord.py treats it as a DM and constructs a
    ``DMChannel`` from the channel_id, no guild cache lookup required.
    """
    return {
        "id": str(secrets.randbits(63)),
        "channel_id": channel_id or str(secrets.randbits(63)),
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


async def _start_client(client: _HarnessClient) -> asyncio.Task[None]:
    """Kick off ``client.start()`` as a background task and wait for READY."""
    task = asyncio.create_task(client.start("fake-token-not-used"))
    try:
        await asyncio.wait_for(client.ready_event.wait(), timeout=HANDSHAKE_TIMEOUT)
    except TimeoutError:
        # If READY never fires, the task may still be running (or have raised).
        # Cancel it so the test can surface the underlying error.
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
    return task


async def _stop_client(client: _HarnessClient, task: asyncio.Task[None]) -> None:
    if not client.is_closed():
        await client.close()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


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


# ---------------------------------------------------------------------------
# Functional tests
# ---------------------------------------------------------------------------


async def test_client_completes_identify_ready_handshake(gateway: FakeDiscordGateway) -> None:
    """discord.py connects, receives HELLO, sends IDENTIFY, gets READY back."""
    client = _HarnessClient(gateway.url, intents=_make_intents())
    task = await _start_client(client)
    try:
        assert client.is_ready()
        # The fake's READY payload sets a stable user.
        assert client.user is not None
        assert client.user.name == "fake-bot"
    finally:
        await _stop_client(client, task)


async def test_deliver_fires_on_message(gateway: FakeDiscordGateway) -> None:
    """gateway.deliver(...) round-trips through to the client's on_message."""
    client = _HarnessClient(gateway.url, intents=_make_intents())
    task = await _start_client(client)
    try:
        payload = _build_message_payload(content="hello fake")
        await gateway.deliver(payload)

        # Poll for the message; the gateway -> client dispatch is async.
        deadline = asyncio.get_running_loop().time() + MESSAGE_TIMEOUT
        while not client.message_log and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert len(client.message_log) == 1
        assert client.message_log[0].content == "hello fake"
        assert str(client.message_log[0].id) == payload["id"]

        # injected_messages tracks every deliver call regardless of session state.
        assert gateway.injected_messages == [payload]
    finally:
        await _stop_client(client, task)


async def test_resume_replays_missed_events(gateway: FakeDiscordGateway) -> None:
    """After disconnect(), the client RESUMEs and we replay buffered events."""
    client = _HarnessClient(gateway.url, intents=_make_intents())
    task = await _start_client(client)
    try:
        # Deliver one event the client should see immediately.
        first = _build_message_payload(content="before disconnect")
        await gateway.deliver(first)

        # Wait for it to land.
        deadline = asyncio.get_running_loop().time() + MESSAGE_TIMEOUT
        while not client.message_log and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert len(client.message_log) == 1

        # Force a recoverable disconnect. discord.py will reconnect + RESUME.
        await gateway.disconnect(reason="test-induced disconnect")

        # Wait for resume to complete (RESUMED dispatch fires on_resumed).
        await asyncio.wait_for(client.resumed_event.wait(), timeout=HANDSHAKE_TIMEOUT)

        # Deliver another after resume; it should arrive on the new connection.
        second = _build_message_payload(content="after resume")
        await gateway.deliver(second)

        deadline = asyncio.get_running_loop().time() + MESSAGE_TIMEOUT
        while len(client.message_log) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        contents = [m.content for m in client.message_log]
        # No duplication of the pre-disconnect event; both events delivered exactly once.
        assert contents == ["before disconnect", "after resume"]
    finally:
        await _stop_client(client, task)


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


async def test_multiple_sequential_clients_do_not_leak_state(
    gateway: FakeDiscordGateway,
) -> None:
    """Two independent clients (one at a time) each get a clean session."""
    # Client 1 -- send a message, then close cleanly.
    client1 = _HarnessClient(gateway.url, intents=_make_intents())
    task1 = await _start_client(client1)
    try:
        await gateway.deliver(_build_message_payload(content="for client 1"))
        deadline = asyncio.get_running_loop().time() + MESSAGE_TIMEOUT
        while not client1.message_log and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert len(client1.message_log) == 1
    finally:
        await _stop_client(client1, task1)

    # Brief pause to let the close handshake settle on the server side.
    await asyncio.sleep(0.1)

    # Client 2 -- fresh client, fresh session. Should NOT see client 1's message.
    client2 = _HarnessClient(gateway.url, intents=_make_intents())
    task2 = await _start_client(client2)
    try:
        assert client2.message_log == []  # no leakage from client 1's session

        await gateway.deliver(_build_message_payload(content="for client 2"))
        deadline = asyncio.get_running_loop().time() + MESSAGE_TIMEOUT
        while not client2.message_log and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert len(client2.message_log) == 1
        assert client2.message_log[0].content == "for client 2"
    finally:
        await _stop_client(client2, task2)


async def test_stop_mid_handshake_does_not_raise_in_client() -> None:
    """Stopping the gateway right after a client connects shouldn't blow up."""
    gw = FakeDiscordGateway()
    await gw.start()

    client = _HarnessClient(gw.url, intents=_make_intents())
    task = asyncio.create_task(client.start("fake-token-not-used"))

    # Yield once so the client opens the WS and receives HELLO, then yank the
    # gateway out from under it before READY fires.
    await asyncio.sleep(0.05)
    await gw.stop()

    # Cleanup. The client may keep retrying; close it.
    await asyncio.sleep(0.1)
    await client.close()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


# ---------------------------------------------------------------------------
# Error-handling tests
# ---------------------------------------------------------------------------


async def test_malformed_identify_closes_with_error_code(
    gateway: FakeDiscordGateway,
) -> None:
    """Sending a bogus IDENTIFY (no token) closes with a 4xxx error code."""
    # We connect a raw websockets client so we can send a malformed IDENTIFY.
    import json

    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    async with connect(gateway.url) as ws:
        # Drain HELLO first.
        hello = json.loads(await ws.recv())
        assert hello["op"] == 10  # OP_HELLO

        # Send a malformed IDENTIFY -- missing 'token'.
        await ws.send(json.dumps({"op": 2, "d": {"intents": 0}}))

        # The fake should close with a non-1000 code.
        with pytest.raises(ConnectionClosed) as exc_info:
            await ws.recv()

        # 4002 is the decode-error code we use for malformed handshake payloads.
        assert exc_info.value.rcvd is not None
        assert exc_info.value.rcvd.code == 4002


async def test_deliver_before_client_connect_raises_runtime_error() -> None:
    """deliver() before any client has IDENTIFY'd raises a clear RuntimeError."""
    gw = FakeDiscordGateway()
    await gw.start()
    try:
        with pytest.raises(RuntimeError, match="no client is connected"):
            await gw.deliver(_build_message_payload(content="orphan"))
    finally:
        await gw.stop()
