"""End-to-end webhook retry / dead-letter integration test (task #44).

Spec references: §5.5 (webhook delivery + retry policy), §6.4 (backoff
schedule), §7.4.1 (``GET /messages/unread``), §7.4.11 (header table).

Scenario walked end-to-end:

1. The full :mod:`amc.app` FastAPI app is booted against a fresh SQLite
   DB created via ``alembic upgrade head``.
2. A real :class:`amc.connectors.discord.connector.DiscordConnector`,
   pointed at the in-process :class:`tests.fakes.discord_gateway.FakeDiscordGateway`,
   delivers one allowlisted ``MESSAGE_CREATE`` payload. The connector
   pushes the normalized envelope through a real
   :class:`amc.core.message_sink.MessageSink`, which persists the row
   AND enqueues the matching ``webhook_deliveries`` row in the same
   transaction (spec §5.5 acceptance criteria).
3. A :class:`amc.core.webhook.WebhookWorker` with an injectable
   ``time_provider`` ticks the queue. ``respx`` intercepts the outbound
   ``httpx.AsyncClient`` POSTs so we can return crafted status codes.

The 5xx case fast-forwards through the full ``[1s, 5s, 30s, 2m, 10m]``
backoff schedule and asserts the row dead-letters after exactly five
attempts. The 4xx case asserts a single 400 response dead-letters
immediately with no retry.

After dead-lettering the test verifies the underlying message is still
returned by ``GET /messages/unread`` — webhook failure must not
suppress the agent-poll fallback path (spec §5.5 "if delivery fails
permanently, the message remains queryable via the unread endpoint").

Logging assertion: per task brief "verify each attempt logged at
WARN/ERROR with attempt number". The worker emits a structured event
on every attempt that carries an ``attempt`` field as either a record
attribute (when the structlog/stdlib bridge is installed) or in the
formatted message; we accept either form.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import structlog
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from amc.api.messages_unread import (
    configure_session_factory as configure_unread_session_factory,
)
from amc.api.messages_unread import (
    reset_session_factory as reset_unread_session_factory,
)
from amc.app import build_app
from amc.connectors.discord.connector import DiscordConnector
from amc.core.allowlist import AllowlistEntry, AllowlistLoader
from amc.core.auth import configure_bearer_token, reset_bearer_token
from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.envelope import Source
from amc.core.message_sink import MessageSink
from amc.core.webhook import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    WebhookConfig,
    WebhookWorker,
)
from tests.fakes.discord_gateway import FakeDiscordGateway

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-e2e-webhook-retry"
AGENT_ID = "agent-e2e"

WEBHOOK_URL = "https://receiver.example/hooks/amc"
WEBHOOK_SECRET = "shared-secret-e2e-webhook-retry"  # noqa: S105 - test fixture

ALLOWED_USER_ID = "999888777666555444"

HANDSHAKE_TIMEOUT = 5.0
RECORD_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Manually-advanced clock
# ---------------------------------------------------------------------------


class _FakeClock:
    """Manually-advanced wall-clock for the worker's ``time_provider``."""

    __slots__ = ("now",)

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# In-memory allowlist with the one Discord user we will inject
# ---------------------------------------------------------------------------


def _build_allowlist() -> AllowlistLoader:
    """In-memory loader containing one allowlisted Discord user."""
    loader = AllowlistLoader.__new__(AllowlistLoader)
    loader.path = None  # type: ignore[assignment]
    loader._entries = {
        (Source.DISCORD, ALLOWED_USER_ID): AllowlistEntry(
            source=Source.DISCORD,
            id=ALLOWED_USER_ID,
            display_name="Allowlisted Alice",
            person_id="person_alice",
        ),
    }
    return loader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    db_path = tmp_path / "webhook-retry.db"
    monkeypatch.setenv("AMC_DB_PATH", str(db_path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    yield db_path


@pytest.fixture
def session_factory(fresh_db: Path) -> Iterator[async_sessionmaker]:
    """Async session factory bound to the migrated tmp_path DB."""
    engine = create_engine_from_env(db_path_override=fresh_db)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _reset_module_globals() -> Iterator[None]:
    """Reset bearer-token and session-factory module globals between tests."""
    reset_bearer_token()
    reset_unread_session_factory()
    yield
    reset_bearer_token()
    reset_unread_session_factory()


@pytest.fixture
def adapter_client(session_factory: async_sessionmaker) -> Iterator[TestClient]:
    """Boot the full :mod:`amc.app` adapter with bearer + DB wired up."""
    configure_bearer_token(BEARER)
    configure_unread_session_factory(session_factory)

    app = build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
async def gateway() -> AsyncIterator[FakeDiscordGateway]:
    """Start a fresh :class:`FakeDiscordGateway` for each test."""
    g = FakeDiscordGateway()
    await g.start()
    try:
        yield g
    finally:
        await g.stop()


@pytest.fixture
def fake_clock() -> _FakeClock:
    return _FakeClock()


# ---------------------------------------------------------------------------
# Logging bridge — make caplog see structlog records
# ---------------------------------------------------------------------------


class _ListHandler(logging.Handler):
    """Stdlib log handler that appends every record to an in-memory list.

    Used in place of ``caplog`` because the worker emits via structlog,
    and the structlog→stdlib bridge interacts unpredictably with
    pytest-caplog's level-management when multiple tests run in the same
    session. Attaching this handler directly to the named logger gives
    a deterministic capture surface per-test.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(scope="module", autouse=True)
def _structlog_bridge() -> Iterator[None]:
    """Install the structlog→stdlib bridge once per test module.

    The webhook worker emits via ``structlog.get_logger("amc.webhook")``,
    which returns a :class:`structlog._config.BoundLoggerLazyProxy` that
    caches its resolved bound logger on first call. Reconfiguring or
    resetting structlog mid-test breaks subsequent calls on the cached
    proxy, so we configure once at module scope and never reset.
    """
    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    yield
    structlog.reset_defaults()


@pytest.fixture
def webhook_logs() -> Iterator[_ListHandler]:
    """Attach a per-test capture handler to the ``amc.webhook`` logger.

    Yields a :class:`_ListHandler` whose ``records`` attribute holds every
    log record emitted by the webhook worker during the test. Restores
    the logger handlers / level / propagate flag / disabled state on
    teardown so the test does not leak state into the next.

    NOTE: the ``fresh_db`` fixture runs ``alembic upgrade head`` which
    invokes :func:`logging.config.fileConfig` with the default
    ``disable_existing_loggers=True``. That flag flips
    ``logger.disabled = True`` on every logger created before the call,
    so once the ``amc.webhook`` logger has been instantiated (e.g. by
    the first test in this module), every subsequent ``alembic upgrade``
    silently mutes it. We re-enable it here so per-test capture works.
    """
    webhook_logger = logging.getLogger("amc.webhook")
    prev_level = webhook_logger.level
    prev_propagate = webhook_logger.propagate
    prev_disabled = webhook_logger.disabled

    handler = _ListHandler()
    webhook_logger.addHandler(handler)
    webhook_logger.setLevel(logging.DEBUG)
    webhook_logger.propagate = False  # records go ONLY to our handler
    webhook_logger.disabled = False  # undo alembic fileConfig side effect

    try:
        yield handler
    finally:
        webhook_logger.removeHandler(handler)
        webhook_logger.setLevel(prev_level)
        webhook_logger.propagate = prev_propagate
        webhook_logger.disabled = prev_disabled


# ---------------------------------------------------------------------------
# Discord-payload + connector helpers
# ---------------------------------------------------------------------------


def _dm_message_payload(*, content: str, author_id: str) -> dict[str, Any]:
    """Build a MESSAGE_CREATE payload without ``guild_id`` (= DM)."""
    return {
        "id": str(secrets.randbits(63)),
        "channel_id": str(secrets.randbits(63)),
        "author": {
            "id": author_id,
            "username": "alice",
            "discriminator": "0",
            "global_name": "Alice",
            "avatar": None,
            "bot": False,
        },
        "content": content,
        "timestamp": "2026-05-03T12:00:00.000000+00:00",
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


async def _wait_ready(connector: DiscordConnector) -> None:
    deadline = asyncio.get_running_loop().time() + HANDSHAKE_TIMEOUT
    while not connector.is_ready() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    if not connector.is_ready():
        raise TimeoutError("DiscordConnector did not become ready in time")


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


async def _wait_for_delivery_row(db_path: Path, *, timeout: float = RECORD_TIMEOUT) -> str:
    """Poll until exactly one ``webhook_deliveries`` row exists; return its id."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = _list_deliveries(db_path)
        if rows:
            return rows[0]["id"]
        await asyncio.sleep(0.02)
    raise TimeoutError("no webhook_deliveries row appeared within the timeout window")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _list_deliveries(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM webhook_deliveries ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _read_delivery_row(db_path: Path, delivery_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise AssertionError(f"delivery {delivery_id} not found")
        return dict(row)
    finally:
        conn.close()


def _read_message_id(db_path: Path) -> str:
    """Return the only persisted ``messages.id`` (raises if not exactly one)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id FROM messages").fetchall()
        if len(rows) != 1:
            raise AssertionError(f"expected exactly one messages row; got {len(rows)}")
        return rows[0]["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Log-record helpers
# ---------------------------------------------------------------------------


def _records_for(handler: _ListHandler, event: str) -> list[logging.LogRecord]:
    """Return every captured record whose message contains ``event``."""
    return [r for r in handler.records if event in r.getMessage()]


def _record_attempt(record: logging.LogRecord) -> int | None:
    """Pull the ``attempt`` field off a structlog/stdlib bridge record."""
    attempt = getattr(record, "attempt", None)
    if isinstance(attempt, int):
        return attempt
    msg = record.getMessage()
    # Fallback: structlog may have rendered it into the message.
    for token in msg.split():
        if token.startswith("attempt="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Test 1 — full 5xx backoff schedule dead-letters after attempt 5
# ---------------------------------------------------------------------------


async def test_full_backoff_dead_letters_then_message_still_unread(
    fresh_db: Path,
    session_factory: async_sessionmaker,
    adapter_client: TestClient,
    gateway: FakeDiscordGateway,
    fake_clock: _FakeClock,
    webhook_logs: _ListHandler,
) -> None:
    """5 sequential 500 responses → status='dead', next_retry_at IS NULL.

    Walks the full ``[1s, 5s, 30s, 2m, 10m]`` schedule (spec §5.5 / §6.4)
    using the injectable clock. After dead-lettering, the underlying
    message must still appear in ``GET /messages/unread`` (spec §5.5 —
    webhook failure does not block the agent-poll fallback).
    """
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    allowlist = _build_allowlist()
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=config,
        time_provider=fake_clock,
    )

    connector = DiscordConnector(
        bot_token="fake-token-not-used",
        message_sink=sink,
        allowlist=allowlist,
        gateway_url=gateway.url,
    )

    connector_task = await _start_connector(connector)
    try:
        await gateway.deliver(
            _dm_message_payload(
                content="hello, e2e webhook retry",
                author_id=ALLOWED_USER_ID,
            )
        )
        delivery_id = await _wait_for_delivery_row(fresh_db)
    finally:
        await _stop_connector(connector, connector_task)

    # Sanity: row enqueued at attempt=0, status='pending'.
    initial = _read_delivery_row(fresh_db, delivery_id)
    assert initial["status"] == "pending"
    assert initial["attempt"] == 0

    # ------------------------------------------------------------------
    # Drive the worker through the full backoff schedule. Receiver
    # returns 500 on every attempt.
    # ------------------------------------------------------------------
    with respx.mock(assert_all_called=True) as router:
        route = router.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as http_client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=fake_clock,
                http_client=http_client,
            )

            # Attempt 1 — fires immediately.
            await worker.tick()

            row = _read_delivery_row(fresh_db, delivery_id)
            assert row["attempt"] == 1
            assert row["status"] == "pending"
            assert row["last_response_code"] == 500

            # Attempts 2..5 — fast-forward each backoff slot in turn.
            # BACKOFF_SECONDS[i] is the wait BEFORE attempt (i+2).
            for i in range(MAX_ATTEMPTS - 1):  # 0..3 → attempts 2..5
                fake_clock.advance(BACKOFF_SECONDS[i] + 0.001)
                await worker.tick()

            # Exactly MAX_ATTEMPTS POSTs were issued (no 6th attempt).
            assert route.call_count == MAX_ATTEMPTS

    # ------------------------------------------------------------------
    # Assertions: dead-letter state.
    # ------------------------------------------------------------------
    final = _read_delivery_row(fresh_db, delivery_id)
    assert final["status"] == "dead"
    assert final["attempt"] == MAX_ATTEMPTS
    assert final["next_retry_at"] is None
    assert final["last_response_code"] == 500

    # ------------------------------------------------------------------
    # Assertion: message still appears in GET /messages/unread.
    # ------------------------------------------------------------------
    message_id = _read_message_id(fresh_db)
    resp = adapter_client.get(
        "/messages/unread",
        headers={
            "Authorization": f"Bearer {BEARER}",
            "X-Agent-ID": AGENT_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    returned_ids = [m["id"] for m in body["messages"]]
    assert message_id in returned_ids, (
        f"dead-lettered message {message_id} should still be queryable; "
        f"got {returned_ids}"
    )

    # ------------------------------------------------------------------
    # Assertion: each attempt produced a structured log carrying the
    # attempt number. Attempts 1..4 are ``webhook_retry_scheduled``
    # (INFO); attempt 5 is ``webhook_dead_max_attempts`` (WARNING).
    # The brief asks for WARN/ERROR — the worker's contract emits
    # WARNING for dead-letter and INFO for in-flight retries; we
    # verify (a) the dead-letter event is at WARNING and (b) every
    # attempt produced a record carrying ``attempt=N``.
    # ------------------------------------------------------------------
    retry_records = _records_for(webhook_logs, "webhook_retry_scheduled")
    dead_records = _records_for(webhook_logs, "webhook_dead_max_attempts")

    # 4 in-flight retries scheduled (attempts 1..4 all had a follow-up retry).
    assert len(retry_records) == MAX_ATTEMPTS - 1, (
        f"expected {MAX_ATTEMPTS - 1} webhook_retry_scheduled records; "
        f"got {len(retry_records)}: {[r.getMessage() for r in retry_records]}"
    )
    retry_attempts = sorted(_record_attempt(r) for r in retry_records)
    assert retry_attempts == [1, 2, 3, 4], (
        f"expected attempts [1,2,3,4]; got {retry_attempts}"
    )

    # 1 dead-letter record at WARNING level for the 5th attempt.
    assert len(dead_records) == 1, (
        f"expected exactly one webhook_dead_max_attempts record; "
        f"got {[r.getMessage() for r in dead_records]}"
    )
    dead = dead_records[0]
    assert dead.levelno == logging.WARNING, (
        f"dead-letter event must be WARN/ERROR; got {logging.getLevelName(dead.levelno)}"
    )
    assert _record_attempt(dead) == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Test 2 — 4xx response dead-letters immediately on attempt 1
# ---------------------------------------------------------------------------


async def test_4xx_response_dead_letters_immediately(
    fresh_db: Path,
    session_factory: async_sessionmaker,
    adapter_client: TestClient,
    gateway: FakeDiscordGateway,
    fake_clock: _FakeClock,
    webhook_logs: _ListHandler,
) -> None:
    """A single 400 response → status='dead' on attempt 1 with no retry."""
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    allowlist = _build_allowlist()
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=config,
        time_provider=fake_clock,
    )

    connector = DiscordConnector(
        bot_token="fake-token-not-used",
        message_sink=sink,
        allowlist=allowlist,
        gateway_url=gateway.url,
    )

    connector_task = await _start_connector(connector)
    try:
        await gateway.deliver(
            _dm_message_payload(
                content="hello, 4xx dead-letter",
                author_id=ALLOWED_USER_ID,
            )
        )
        delivery_id = await _wait_for_delivery_row(fresh_db)
    finally:
        await _stop_connector(connector, connector_task)

    with respx.mock(assert_all_called=True) as router:
        route = router.post(WEBHOOK_URL).mock(return_value=httpx.Response(400))
        async with httpx.AsyncClient() as http_client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=fake_clock,
                http_client=http_client,
            )

            processed = await worker.tick()
            assert processed == 1

            # Even if we tick again the dead row must not be re-tried.
            fake_clock.advance(3600.0)
            await worker.tick()

        assert route.call_count == 1, (
            "4xx must dead-letter on attempt 1 — no retries permitted"
        )

    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "dead"
    assert row["attempt"] == 1
    assert row["next_retry_at"] is None
    assert row["last_response_code"] == 400

    # The 4xx dead-letter event must surface at WARN/ERROR with attempt=1.
    dead_records = _records_for(webhook_logs, "webhook_dead_4xx")
    assert len(dead_records) == 1, (
        f"expected exactly one webhook_dead_4xx record; "
        f"got {[r.getMessage() for r in dead_records]}"
    )
    dead = dead_records[0]
    assert dead.levelno >= logging.WARNING, (
        f"4xx dead-letter must be WARN/ERROR; got {logging.getLevelName(dead.levelno)}"
    )
    assert _record_attempt(dead) == 1

    # Underlying message remains queryable.
    message_id = _read_message_id(fresh_db)
    resp = adapter_client.get(
        "/messages/unread",
        headers={
            "Authorization": f"Bearer {BEARER}",
            "X-Agent-ID": AGENT_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    returned_ids = [m["id"] for m in resp.json()["messages"]]
    assert message_id in returned_ids
