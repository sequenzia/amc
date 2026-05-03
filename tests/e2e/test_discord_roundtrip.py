"""Discord round-trip end-to-end test (task #43).

This is the centerpiece of spec §9.1 Checkpoint Gate. It walks the full
Discord path through every layer the adapter ships:

1. The full :mod:`amc.app` FastAPI adapter is booted on an ephemeral
   port, fronted by an in-process :class:`uvicorn.Server` so a real HTTP
   stack handles every request (not the FastAPI in-process TestClient).
2. ``AMC_WEBHOOK_URL`` points at a separate in-process Starlette receiver
   running on a second ephemeral port. The receiver records every POST
   and verifies the ``X-AMC-Signature`` HMAC against the shared secret;
   any mismatch fails the test loud.
3. A real :class:`amc.connectors.discord.connector.DiscordConnector`
   bridges :class:`tests.fakes.discord_gateway.FakeDiscordGateway`
   (inbound) and :class:`tests.fakes.discord_rest.FakeDiscordRest`
   (outbound) into the adapter's :class:`amc.core.message_sink.MessageSink`.
4. A real :class:`amc.core.webhook.WebhookWorker` ticks the queue so the
   recorded ``webhook_deliveries`` row reaches ``status='delivered'``.

The single ``test_discord_roundtrip_full_flow`` exercises:

* Inbound: ``gateway.deliver(MESSAGE_CREATE)`` for an allowlisted user →
  ``messages`` row inserted with ``allowlist_status='allowed'`` →
  receiver gets POST with valid HMAC → ``webhook_deliveries.status =
  'delivered'``.
* Agent poll: ``GET /messages/unread`` (with bearer + X-Agent-ID)
  returns the envelope.
* Outbound: ``POST /messages/send`` with ``Idempotency-Key`` →
  ``FakeDiscordRest.sent_messages`` records the call → response carries
  ``message_id`` + ``sent_at``.
* Mark-read isolation: ``POST /messages/mark_read`` for one agent hides
  the message from that agent's subsequent ``/messages/unread`` while a
  different ``X-Agent-ID`` still sees it (per-agent cursor, spec §5.3).

Implementation choices
----------------------

* **Live HTTP client**: we use :class:`httpx.AsyncClient` against the
  uvicorn server URL rather than ``fastapi.testclient.TestClient`` so
  the test exercises the same wire path operators see.
* **Real receiver**: the brief explicitly asks for "in-process FastAPI
  receiver: a small starlette app that records POST + verifies HMAC".
  We mount a Starlette ``Route`` directly to keep the dependency graph
  flat; the same uvicorn server pattern handles both endpoints.
* **HMAC verification on the receiver**: the receiver re-computes the
  HMAC using :func:`amc.core.webhook.compute_signature` over the raw
  bytes the request carried, so the verification path is the canonical
  one a third-party receiver would implement.
* **No idempotency replay**: each test uses a fresh ``Idempotency-Key``
  to avoid coupling to the cache; the cache is exercised separately by
  :mod:`tests.api.test_messages_send` and :mod:`tests.core.test_idempotency`.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from amc.api.messages_mark_read import (
    configure_session_factory as configure_mark_read_session_factory,
)
from amc.api.messages_mark_read import (
    reset_session_factory as reset_mark_read_session_factory,
)
from amc.api.messages_send import (
    configure_discord_connector,
    configure_rate_limiter,
    reset_discord_connector,
    reset_rate_limiter,
)
from amc.api.messages_send import (
    configure_session_factory as configure_send_session_factory,
)
from amc.api.messages_send import (
    reset_session_factory as reset_send_session_factory,
)
from amc.api.messages_unread import (
    configure_session_factory as configure_unread_session_factory,
)
from amc.api.messages_unread import (
    reset_session_factory as reset_unread_session_factory,
)
from amc.app import build_app
from amc.connectors.discord.connector import DiscordConnector
from amc.core.auth import configure_bearer_token, reset_bearer_token
from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.envelope import Source
from amc.core.idempotency import (
    IdempotencyStore,
    configure_idempotency_store,
    reset_idempotency_store,
)
from amc.core.message_sink import MessageSink
from amc.core.rate_limit import RateLimiter
from amc.core.webhook import WebhookConfig, WebhookWorker, compute_signature
from tests.e2e._helpers import (
    apply_alembic_head,
    build_inmemory_allowlist,
    make_dm_payload,
)
from tests.fakes.discord_gateway import FakeDiscordGateway
from tests.fakes.discord_rest import FakeDiscordRest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-e2e-discord-roundtrip"  # noqa: S105 - test fixture
WEBHOOK_SECRET = "shared-secret-discord-roundtrip-e2e"  # noqa: S105 - test fixture

AGENT_ID_PRIMARY = "agent-discord-rt-primary"
AGENT_ID_SECONDARY = "agent-discord-rt-secondary"

ALLOWED_DISCORD_USER_ID = "777666555444333222"
DISCORD_CHANNEL_ID = "888777666555444333"

GATEWAY_HANDSHAKE_TIMEOUT = 5.0
RECORD_TIMEOUT = 5.0
DELIVERED_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# In-process receiver — a tiny Starlette app that records POSTs + checks HMAC
# ---------------------------------------------------------------------------


class _WebhookReceiver:
    """Records every POST and validates the HMAC.

    The receiver is intentionally tiny: it owns a single ``/hooks/amc``
    route, appends a record dict to :attr:`received` for each POST, and
    sets :attr:`signature_failures` if any inbound HMAC mismatches. Any
    failure is also surfaced as a 400 response so the worker dead-letters
    immediately rather than silently retrying past the test budget.
    """

    __slots__ = ("received", "signature_failures", "secret")

    def __init__(self, secret: str) -> None:
        self.received: list[dict[str, Any]] = []
        self.signature_failures: list[str] = []
        self.secret = secret

    async def handle(self, request: Request) -> JSONResponse:
        body = await request.body()
        signature_header = request.headers.get("x-amc-signature", "")
        expected = compute_signature(self.secret, body)
        ok = signature_header == expected
        record = {
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": body,
            "signature_ok": ok,
        }
        self.received.append(record)
        if not ok:
            self.signature_failures.append(
                f"expected {expected} got {signature_header!r}"
            )
            return JSONResponse({"error": "bad signature"}, status_code=400)
        return JSONResponse({"ok": True}, status_code=200)


def _build_receiver_app(receiver: _WebhookReceiver) -> Starlette:
    return Starlette(routes=[Route("/hooks/amc", receiver.handle, methods=["POST"])])


# ---------------------------------------------------------------------------
# Uvicorn helpers — bind an ephemeral port and run the server in the same loop
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Reserve and immediately release an ephemeral port, returning its number.

    Uvicorn rebinds when started; a momentary race with another process is
    acceptable for tests but we keep the window short by binding in the same
    function call.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _UvicornServer:
    """Run a uvicorn server in a background asyncio task."""

    __slots__ = ("server", "task", "host", "port")

    def __init__(self, app: Any, host: str = "127.0.0.1", port: int = 0) -> None:
        if port == 0:
            port = _free_port()
        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self.server = uvicorn.Server(config)
        self.task: asyncio.Task[None] | None = None
        self.host = host
        self.port = port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        self.task = asyncio.create_task(self.server.serve())
        # Wait until the server says it's ready to accept connections.
        deadline = asyncio.get_running_loop().time() + 5.0
        while not self.server.started:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"uvicorn server on :{self.port} did not start")
            if self.task.done():
                # Surface uvicorn's startup error rather than time out.
                self.task.result()
                raise RuntimeError("uvicorn server task exited before ready")
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        self.server.should_exit = True
        if self.task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self.task, timeout=5.0)
            if not self.task.done():
                self.task.cancel()
                with contextlib.suppress(BaseException):
                    await self.task


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _list_messages(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, source, channel_id, allowlist_status, direction, text, "
            "       message_ts FROM messages ORDER BY message_ts ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _read_delivery_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, message_id, attempt, status, last_response_code, "
            "       next_retry_at FROM webhook_deliveries ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------


async def _wait_until(
    predicate: Any,
    *,
    timeout: float,
    label: str,
    interval: float = 0.025,
) -> None:
    """Poll ``predicate()`` until it returns truthy or raise ``TimeoutError``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out waiting for: {label}")


async def _wait_for_discord_ready(
    connector: DiscordConnector, task: asyncio.Task[None]
) -> None:
    deadline = asyncio.get_running_loop().time() + GATEWAY_HANDSHAKE_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if connector.is_ready():
            return
        if task.done():
            task.result()
            raise RuntimeError("DiscordConnector task exited before READY")
        await asyncio.sleep(0.02)
    raise TimeoutError("DiscordConnector did not become ready in time")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    db_path = tmp_path / "discord-roundtrip.db"
    monkeypatch.setenv("AMC_DB_PATH", str(db_path))
    apply_alembic_head(ALEMBIC_INI, db_path)
    yield db_path


@pytest.fixture
def session_factory(fresh_db: Path) -> Iterator[async_sessionmaker]:
    engine: AsyncEngine = create_engine_from_env(db_path_override=fresh_db)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _reset_module_globals() -> Iterator[None]:
    """Reset every module-level cache the adapter routes resolve through."""
    reset_bearer_token()
    reset_send_session_factory()
    reset_unread_session_factory()
    reset_mark_read_session_factory()
    reset_rate_limiter()
    reset_discord_connector()
    reset_idempotency_store()
    yield
    reset_bearer_token()
    reset_send_session_factory()
    reset_unread_session_factory()
    reset_mark_read_session_factory()
    reset_rate_limiter()
    reset_discord_connector()
    reset_idempotency_store()


@pytest.fixture
async def gateway() -> AsyncIterator[FakeDiscordGateway]:
    g = FakeDiscordGateway()
    await g.start()
    try:
        yield g
    finally:
        await g.stop()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_discord_roundtrip_full_flow(
    fresh_db: Path,
    session_factory: async_sessionmaker,
    gateway: FakeDiscordGateway,
) -> None:
    """End-to-end Discord round-trip — see module docstring for the full flow."""

    # ------------------------------------------------------------------
    # Allowlist + sink + webhook config
    # ------------------------------------------------------------------
    allowlist = build_inmemory_allowlist(
        [
            (
                Source.DISCORD,
                ALLOWED_DISCORD_USER_ID,
                "Allowlisted Alice",
                "person_alice",
            )
        ]
    )

    receiver = _WebhookReceiver(secret=WEBHOOK_SECRET)
    receiver_server = _UvicornServer(_build_receiver_app(receiver))
    await receiver_server.start()

    try:
        webhook_url = f"{receiver_server.url}/hooks/amc"
        webhook_config = WebhookConfig(url=webhook_url, secret=WEBHOOK_SECRET)

        sink = MessageSink(
            session_factory=session_factory,
            allowlist=allowlist,
            webhook_config=webhook_config,
        )

        # ------------------------------------------------------------------
        # Discord connector — real client pointed at the fake gateway
        # ------------------------------------------------------------------
        connector = DiscordConnector(
            bot_token="fake-token-not-used",
            message_sink=sink,
            allowlist=allowlist,
            gateway_url=gateway.url,
        )
        connector_task = asyncio.create_task(
            connector.start("fake-token-not-used"),
            name="amc.test.discord_roundtrip.start",
        )
        await _wait_for_discord_ready(connector, connector_task)

        # ------------------------------------------------------------------
        # Adapter wiring + uvicorn server
        # ------------------------------------------------------------------
        configure_bearer_token(BEARER)
        configure_send_session_factory(session_factory)
        configure_unread_session_factory(session_factory)
        configure_mark_read_session_factory(session_factory)
        configure_rate_limiter(RateLimiter(rps=100.0, burst=100))
        configure_idempotency_store(IdempotencyStore(session_factory=session_factory))
        configure_discord_connector(connector)

        adapter_app = build_app()
        adapter_server = _UvicornServer(adapter_app)
        await adapter_server.start()

        try:
            # ------------------------------------------------------------------
            # 1. Drive an inbound MESSAGE_CREATE for the allowlisted user.
            # ------------------------------------------------------------------
            await gateway.deliver(
                make_dm_payload(
                    message_id=str(secrets.randbits(63)),
                    channel_id=DISCORD_CHANNEL_ID,
                    author_id=ALLOWED_DISCORD_USER_ID,
                    content="hello, end-to-end discord world",
                )
            )

            # Wait for messages row.
            await _wait_until(
                lambda: any(
                    m["allowlist_status"] == "allowed"
                    and m["source"] == "discord"
                    and m["direction"] == "inbound"
                    for m in _list_messages(fresh_db)
                ),
                timeout=RECORD_TIMEOUT,
                label="inbound discord message persisted with allowlist_status='allowed'",
            )

            messages = _list_messages(fresh_db)
            inbound = next(
                m for m in messages if m["direction"] == "inbound" and m["source"] == "discord"
            )
            assert inbound["allowlist_status"] == "allowed", (
                f"expected allowlist_status='allowed'; got {inbound!r}"
            )
            assert inbound["text"] == "hello, end-to-end discord world"
            inbound_id = inbound["id"]

            # ------------------------------------------------------------------
            # 2. Drive the webhook worker so the queued row is delivered to the
            #    in-process receiver. We tick in a loop until the row reaches
            #    'delivered' or the timeout fires (the worker schedules retries
            #    on failure but a 200 response should land on attempt 1).
            # ------------------------------------------------------------------
            async with httpx.AsyncClient() as worker_http:
                worker = WebhookWorker(
                    session_factory=session_factory,
                    config=webhook_config,
                    http_client=worker_http,
                )
                await worker.tick()

                await _wait_until(
                    lambda: any(
                        d["status"] == "delivered" and d["message_id"] == inbound_id
                        for d in _read_delivery_rows(fresh_db)
                    ),
                    timeout=DELIVERED_TIMEOUT,
                    label="webhook_deliveries.status='delivered' for inbound message",
                )

            # Receiver got the POST with a valid HMAC.
            assert receiver.signature_failures == [], (
                f"webhook receiver reported HMAC mismatches: {receiver.signature_failures}"
            )
            assert len(receiver.received) >= 1, (
                "in-process webhook receiver did not record any POSTs"
            )
            posted = receiver.received[-1]
            assert posted["signature_ok"] is True
            assert posted["headers"]["x-amc-message-id"] == inbound_id
            assert posted["headers"]["content-type"] == "application/json"
            assert posted["headers"]["x-amc-attempt"] == "1"

            # Delivery row final state.
            delivery = next(
                d
                for d in _read_delivery_rows(fresh_db)
                if d["message_id"] == inbound_id
            )
            assert delivery["status"] == "delivered"
            assert delivery["last_response_code"] == 200
            assert delivery["next_retry_at"] is None

            # ------------------------------------------------------------------
            # 3. GET /messages/unread (bearer + X-Agent-ID) returns the envelope.
            # ------------------------------------------------------------------
            base_url = adapter_server.url
            async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as agent_http:
                unread_resp = await agent_http.get(
                    "/messages/unread",
                    headers={
                        "Authorization": f"Bearer {BEARER}",
                        "X-Agent-ID": AGENT_ID_PRIMARY,
                    },
                )
                assert unread_resp.status_code == 200, unread_resp.text
                unread_body = unread_resp.json()
                assert "messages" in unread_body
                envelope_ids = [m["id"] for m in unread_body["messages"]]
                assert inbound_id in envelope_ids, (
                    f"expected {inbound_id} in unread envelopes; got {envelope_ids}"
                )

                envelope = next(
                    m for m in unread_body["messages"] if m["id"] == inbound_id
                )
                assert envelope["source"] == "discord"
                assert envelope["channel_type"] == "dm"
                assert envelope["channel_id"] == f"discord:dm:{DISCORD_CHANNEL_ID}"
                assert envelope["sender"]["id"] == ALLOWED_DISCORD_USER_ID
                assert envelope["text"] == "hello, end-to-end discord world"
                assert envelope["direction"] == "inbound"

                # ------------------------------------------------------------------
                # 4. POST /messages/send with Idempotency-Key. The DiscordConnector's
                #    real send path runs through aiohttp; we patch it via FakeDiscordRest
                #    so the REST POST is captured rather than escaping to discord.com.
                # ------------------------------------------------------------------
                outbound_text = "outbound from e2e discord roundtrip"
                with FakeDiscordRest() as rest:
                    send_resp = await agent_http.post(
                        "/messages/send",
                        headers={
                            "Authorization": f"Bearer {BEARER}",
                            "X-Agent-ID": AGENT_ID_PRIMARY,
                            "Idempotency-Key": secrets.token_hex(16),
                            "Content-Type": "application/json",
                        },
                        json={
                            "channel_id": f"discord:dm:{DISCORD_CHANNEL_ID}",
                            "text": outbound_text,
                        },
                    )
                    assert send_resp.status_code == 200, send_resp.text
                    send_body = send_resp.json()
                    assert "message_id" in send_body
                    assert send_body["message_id"].startswith("msg_")
                    assert "sent_at" in send_body
                    assert send_body["sent_at"].endswith("Z")

                    # FakeDiscordRest captured the outbound POST.
                    assert len(rest.sent_messages) == 1, (
                        f"expected exactly one Discord REST send; "
                        f"got {len(rest.sent_messages)}: {rest.sent_messages}"
                    )
                    sent = rest.sent_messages[0]
                    assert sent["method"] == "POST"
                    assert sent["channel_id"] == int(DISCORD_CHANNEL_ID)
                    assert sent["content"] == outbound_text

                # ------------------------------------------------------------------
                # 5. POST /messages/mark_read for AGENT_ID_PRIMARY hides the inbound
                #    from that agent; AGENT_ID_SECONDARY still sees it (per-agent
                #    cursor — spec §5.3).
                # ------------------------------------------------------------------
                mark_resp = await agent_http.post(
                    "/messages/mark_read",
                    headers={
                        "Authorization": f"Bearer {BEARER}",
                        "X-Agent-ID": AGENT_ID_PRIMARY,
                        "Content-Type": "application/json",
                    },
                    json={"message_ids": [inbound_id]},
                )
                assert mark_resp.status_code == 200, mark_resp.text
                assert mark_resp.json() == {"marked_count": 1}

                # Primary agent: no longer sees the inbound message.
                primary_after = await agent_http.get(
                    "/messages/unread",
                    headers={
                        "Authorization": f"Bearer {BEARER}",
                        "X-Agent-ID": AGENT_ID_PRIMARY,
                    },
                )
                assert primary_after.status_code == 200
                primary_ids = [m["id"] for m in primary_after.json()["messages"]]
                assert inbound_id not in primary_ids, (
                    f"AGENT_ID_PRIMARY should not see {inbound_id} after mark_read; "
                    f"got {primary_ids}"
                )

                # Secondary agent: still sees the inbound message (per-agent cursor).
                secondary_after = await agent_http.get(
                    "/messages/unread",
                    headers={
                        "Authorization": f"Bearer {BEARER}",
                        "X-Agent-ID": AGENT_ID_SECONDARY,
                    },
                )
                assert secondary_after.status_code == 200
                secondary_ids = [m["id"] for m in secondary_after.json()["messages"]]
                assert inbound_id in secondary_ids, (
                    f"AGENT_ID_SECONDARY should still see {inbound_id} (per-agent "
                    f"cursor); got {secondary_ids}"
                )
        finally:
            await adapter_server.stop()

        # Tear down the discord connector.
        with contextlib.suppress(Exception):
            await connector.close()
        connector_task.cancel()
        with contextlib.suppress(BaseException):
            await connector_task

    finally:
        await receiver_server.stop()
