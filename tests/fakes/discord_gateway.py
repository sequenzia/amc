"""In-process fake Discord gateway WebSocket server for tests.

Speaks enough of the Discord gateway protocol (HELLO -> IDENTIFY -> READY,
MESSAGE_CREATE dispatch, RESUME) to drive ``discord.py`` end-to-end without a
real bot token or network access.

Usage:

    gateway = FakeDiscordGateway()
    await gateway.start()
    try:
        # point discord.py at gateway.url, drive a connection, then:
        await gateway.deliver({
            "id": "111",
            "channel_id": "222",
            "author": {"id": "333", "username": "alice", ...},
            "content": "hello",
        })
    finally:
        await gateway.stop()

The gateway intentionally does not implement: heartbeat ACK timeouts, presence,
voice, sharding, compression (frames are sent as TEXT, so ``discord.py``'s
zlib/zstd decompression path is bypassed), or any of the dispatch events beyond
``READY``, ``RESUMED``, and ``MESSAGE_CREATE``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

# --- Gateway opcodes (mirror discord.py's DiscordWebSocket constants) ---
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Heartbeat interval (ms) -- mirrors what real Discord sends so client behavior
# is realistic without making tests slow.
DEFAULT_HEARTBEAT_INTERVAL_MS = 41_250

# Close codes 4000-4999 are "recoverable": discord.py will attempt RESUME.
# 4004 (auth failed), 4010-4014 are non-recoverable -- avoid those for
# disconnect()-driven tests.
CLOSE_CODE_RECOVERABLE = 4000
CLOSE_CODE_DECODE_ERROR = 4002

# Buffer up to this many recent dispatch events per session for replay on
# RESUME. Real Discord keeps a short window; we keep a generous one so tests
# don't drop events.
RESUME_BUFFER_LIMIT = 256


@dataclass
class _SessionState:
    """Per-session bookkeeping retained across reconnects so RESUME can replay."""

    session_id: str
    sent_events: list[dict[str, Any]] = field(default_factory=list)
    last_seq: int = 0


class FakeDiscordGateway:
    """In-process fake Discord gateway server.

    Public API:
      - ``start()`` / ``stop()`` lifecycle
      - ``url`` property returning a ``ws://127.0.0.1:<port>`` URL
      - ``deliver(payload)`` dispatches a MESSAGE_CREATE
      - ``disconnect(reason=...)`` forces the active client to reconnect
      - ``injected_messages`` is a log of every payload deliver()'d (assertion aid)
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        heartbeat_interval_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS,
    ) -> None:
        self._host = host
        self._heartbeat_interval_ms = heartbeat_interval_ms

        self._server: Server | None = None
        self._port: int | None = None

        # Currently-attached connection (the gateway only supports one client
        # at a time, mirroring how discord.py's main shard connects).
        self._active: ServerConnection | None = None
        self._active_session: _SessionState | None = None

        # Persistent per-session state keyed by session_id, so a RESUME after
        # disconnect() can find the buffered events.
        self._sessions: dict[str, _SessionState] = {}

        # Public assertion log -- every payload passed to deliver(), in order.
        self.injected_messages: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind to an ephemeral port and start serving connections."""
        if self._server is not None:
            raise RuntimeError("FakeDiscordGateway is already started")

        self._server = await serve(
            self._handler,
            host=self._host,
            port=0,  # ephemeral
            # Disable per-message-deflate; we send plain text frames so
            # discord.py's zlib-stream decompressor is bypassed.
            compression=None,
        )

        # ``serve`` returns once the listening socket is bound; pull the port.
        sockets = list(self._server.sockets)
        if not sockets:
            raise RuntimeError("FakeDiscordGateway failed to bind a socket")
        self._port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Close all active connections and stop the server."""
        if self._server is None:
            return

        self._server.close()
        # close() schedules close on all open connections; wait_closed() drains.
        with contextlib.suppress(asyncio.CancelledError):
            await self._server.wait_closed()
        self._server = None
        self._port = None
        self._active = None
        self._active_session = None

    @property
    def url(self) -> str:
        """WebSocket URL the discord.py client should connect to."""
        if self._port is None:
            raise RuntimeError("FakeDiscordGateway is not started")
        return f"ws://{self._host}:{self._port}"

    # ------------------------------------------------------------------
    # Driver API
    # ------------------------------------------------------------------

    async def deliver(self, payload: dict[str, Any]) -> None:
        """Dispatch a MESSAGE_CREATE event to the connected client.

        ``payload`` is the inner ``d`` field of the event -- whatever shape
        discord.py expects for a MESSAGE_CREATE (id, channel_id, author, content,
        ...). The fake stamps a monotonic sequence number and buffers the event
        so a subsequent RESUME can replay it.
        """
        # Always log the inject attempt so tests can assert ordering even after
        # the gateway has rotated sessions.
        self.injected_messages.append(payload)

        if self._active is None or self._active_session is None:
            raise RuntimeError(
                "FakeDiscordGateway.deliver(): no client is connected. "
                "Start the gateway and let a client complete IDENTIFY/READY "
                "before delivering messages."
            )

        await self._dispatch(self._active, self._active_session, "MESSAGE_CREATE", payload)

    async def disconnect(self, *, reason: str = "fake gateway disconnect") -> None:
        """Force-close the active connection with a recoverable code (4000).

        ``discord.py`` treats codes in 4000-4999 (excluding 4004/4010-4014) as
        recoverable and will reconnect + RESUME using the last session_id and
        sequence number it observed.
        """
        active = self._active
        if active is None:
            return

        # Drop the active reference *before* closing so a re-entrant handler
        # cleanup path doesn't try to clear it twice.
        self._active = None
        self._active_session = None
        with contextlib.suppress(ConnectionClosed):
            await active.close(code=CLOSE_CODE_RECOVERABLE, reason=reason)

    # ------------------------------------------------------------------
    # Connection handler (one per inbound WS connection)
    # ------------------------------------------------------------------

    async def _handler(self, conn: ServerConnection) -> None:
        # Send HELLO immediately; discord.py blocks on poll_event() waiting for
        # exactly this opcode before sending IDENTIFY / RESUME.
        try:
            await self._send(conn, {
                "op": OP_HELLO,
                "d": {"heartbeat_interval": self._heartbeat_interval_ms},
            })
        except ConnectionClosed:
            return

        session: _SessionState | None = None
        try:
            async for raw in conn:
                # Accept TEXT or BINARY (discord.py sends TEXT JSON for ops).
                if isinstance(raw, bytes | bytearray):
                    text = bytes(raw).decode("utf-8", errors="replace")
                else:
                    text = raw

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    await conn.close(code=CLOSE_CODE_DECODE_ERROR, reason="invalid JSON")
                    return

                op = msg.get("op")
                data = msg.get("d")

                if op == OP_HEARTBEAT:
                    # Real Discord ACKs every heartbeat so the client doesn't
                    # tear down the connection thinking we vanished.
                    await self._send(conn, {"op": OP_HEARTBEAT_ACK, "d": None})
                    continue

                if op == OP_IDENTIFY:
                    if not isinstance(data, dict) or "token" not in data:
                        # Malformed IDENTIFY -- close with a clear error code.
                        await conn.close(
                            code=CLOSE_CODE_DECODE_ERROR,
                            reason="malformed IDENTIFY: missing token",
                        )
                        return

                    session = self._begin_session(conn)
                    await self._dispatch_ready(conn, session)
                    continue

                if op == OP_RESUME:
                    session = await self._handle_resume(conn, data)
                    if session is None:
                        # _handle_resume already closed/invalidated the conn.
                        return
                    continue

                # Any other op (PRESENCE, VOICE_STATE, etc.) is silently
                # accepted so the client doesn't error out -- we don't model
                # those flows.

        except ConnectionClosed:
            pass
        finally:
            # Only drop the active reference if it's still us; disconnect()
            # may have rotated it already.
            if self._active is conn:
                self._active = None
                self._active_session = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _begin_session(self, conn: ServerConnection) -> _SessionState:
        session_id = secrets.token_hex(16)
        session = _SessionState(session_id=session_id)
        self._sessions[session_id] = session
        self._active = conn
        self._active_session = session
        return session

    async def _dispatch_ready(self, conn: ServerConnection, session: _SessionState) -> None:
        ready_payload: dict[str, Any] = {
            "v": 10,
            "user": {
                "id": "1000000000000000001",
                "username": "fake-bot",
                "discriminator": "0",
                "global_name": "fake-bot",
                "avatar": None,
                "bot": True,
                "system": False,
                "mfa_enabled": False,
                "verified": True,
                "email": None,
                "flags": 0,
                "public_flags": 0,
            },
            "guilds": [],
            "session_id": session.session_id,
            # Point RESUME back at this same fake server.
            "resume_gateway_url": self.url,
            "shard": [0, 1],
            "application": {
                "id": "1000000000000000002",
                "flags": 0,
            },
        }
        await self._dispatch(conn, session, "READY", ready_payload)

    async def _handle_resume(
        self,
        conn: ServerConnection,
        data: Any,
    ) -> _SessionState | None:
        if not isinstance(data, dict):
            await conn.close(code=CLOSE_CODE_DECODE_ERROR, reason="malformed RESUME")
            return None

        session_id = data.get("session_id")
        client_seq = data.get("seq")

        session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            # Tell the client its session is dead; discord.py will reconnect
            # and IDENTIFY fresh.
            await self._send(conn, {"op": OP_INVALID_SESSION, "d": False})
            await conn.close(code=CLOSE_CODE_RECOVERABLE, reason="invalid session")
            return None

        # Resumed session takes the active slot.
        self._active = conn
        self._active_session = session

        # Replay everything the client hasn't acknowledged yet. discord.py
        # tracks ``self.sequence`` from each dispatch, so anything > client_seq
        # is unacknowledged from its perspective.
        try:
            client_seq_int = int(client_seq) if client_seq is not None else 0
        except (TypeError, ValueError):
            client_seq_int = 0

        replay = [event for event in session.sent_events if event["s"] > client_seq_int]
        for event in replay:
            await conn.send(json.dumps(event))

        # Acknowledge the resume itself.
        await self._dispatch(conn, session, "RESUMED", {})
        return session

    # ------------------------------------------------------------------
    # Low-level send helpers
    # ------------------------------------------------------------------

    async def _send(self, conn: ServerConnection, frame: dict[str, Any]) -> None:
        await conn.send(json.dumps(frame))

    async def _dispatch(
        self,
        conn: ServerConnection,
        session: _SessionState,
        event: str,
        data: dict[str, Any],
    ) -> None:
        session.last_seq += 1
        frame = {
            "op": OP_DISPATCH,
            "t": event,
            "s": session.last_seq,
            "d": data,
        }
        # Buffer for RESUME replay (cap at RESUME_BUFFER_LIMIT to avoid unbounded
        # growth in long-running tests).
        session.sent_events.append(frame)
        if len(session.sent_events) > RESUME_BUFFER_LIMIT:
            del session.sent_events[: len(session.sent_events) - RESUME_BUFFER_LIMIT]
        await conn.send(json.dumps(frame))


__all__: Iterable[str] = ("FakeDiscordGateway",)
