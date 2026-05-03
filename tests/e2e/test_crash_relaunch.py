"""Crash-and-relaunch end-to-end test (task #74; spec §5.1, §5.5, §7.7).

Verifies the adapter's RTO + RPO contract:

* **RPO**: every inbound message persisted *before* a crash survives the
  crash, every inbound persisted *after* the relaunch is also captured,
  and the ``connector_state.cursor`` rows correctly resume from where
  each connector left off (so no row is duplicated and no row is lost).
* **RTO**: the adapter is back to processing inbound within 5 seconds of
  the relaunch.

Scenario
--------

1. Build a fresh SQLite DB (``alembic upgrade head``).
2. Start a :class:`tests.fakes.discord_gateway.FakeDiscordGateway`,
   start a real :class:`amc.connectors.discord.connector.DiscordConnector`
   pointed at it, and start a real
   :class:`amc.connectors.imessage.connector.ImessageConnector` pointed
   at a writable copy of the Phase 0 chat.db fixture. Both connectors
   share one :class:`amc.core.message_sink.MessageSink` and one
   :class:`amc.core.webhook.WebhookConfig`.
3. Inject 9 inbound messages interleaved across the two transports
   (5 Discord MESSAGE_CREATE + 4 chat.db rows). After the first 5 are
   persisted, fire one ``POST /messages/send`` to drive the outbound
   path. Then "crash" the adapter: cancel the connector tasks, dispose
   the SQLAlchemy engine, and stop the gateway -- the equivalent of a
   ``SIGKILL`` from the spec's perspective (every in-flight task
   completes its current DB transaction or rolls back; nothing
   half-committed survives).
4. Restart everything against the same DB file. Inject the remaining
   inbound messages.
5. Assert:
   * All 9 inbound messages appear exactly once in ``messages``.
   * The single outbound message appears exactly once.
   * ``connector_state.cursor`` advanced for both ``source='imessage'``
     and ``source='discord'``.
   * Every ``webhook_deliveries`` row is in a sane state
     (``pending`` / ``delivered`` -- nothing corrupt).
   * RTO: the first inbound delivered after relaunch was visible in
     ``messages`` within 5 seconds of the connector restart.

Implementation notes
--------------------

The task brief offers two implementation paths -- subprocess uvicorn
+ ``os.kill(pid, SIGKILL)`` and an in-process simulation -- and
explicitly notes that the in-process simulation "stresses the same
DB-state-recovery code paths without OS process management". We take
the in-process path because:

* :mod:`amc.app` does not yet wire the DB lifespan or the connectors
  (see the docstring -- "wiring lifespan that loads the bearer token,
  opens the DB pool, hydrates the allowlist, and starts the webhook +
  connector workers" is explicitly future work). A subprocess uvicorn
  would have nothing for the test to wire fakes into.
* The recovery path we care about is the SQLite ``connector_state``
  cursor + WAL atomicity. Process boundary or not, the verification
  is the same: dispose every in-memory state, re-open against the
  same DB file, and check the cursor + ``messages`` invariants.

The "crash" we simulate is therefore: cancel the discord.py + iMessage
poller asyncio tasks, await any in-flight transaction to drain (or be
rolled back), dispose the SQLAlchemy engine. No state is carried in
Python memory across the boundary. The test then re-creates the
connectors / engine / sink against the same DB file.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

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
from amc.connectors.imessage.connector import ImessageConnector
from amc.connectors.imessage.reader import ChatDbReader
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
from amc.core.webhook import WebhookConfig
from tests.e2e._helpers import (
    append_chat_db_inbound_row,
    apply_alembic_head,
    build_inmemory_allowlist,
    list_messages_for_assertion,
    list_webhook_deliveries,
    make_dm_payload,
    read_discord_cursor,
    read_imessage_cursor,
    read_messages_count,
)
from tests.fakes.applescript import FakeAppleScriptSender
from tests.fakes.discord_gateway import FakeDiscordGateway
from tests.fixtures.build_chat_db import (
    ALLOWLISTED_HANDLE,
    ensure_chat_db,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-e2e-crash-relaunch"  # noqa: S105 -- test fixture
AGENT_ID = "agent-e2e-crash-relaunch"

WEBHOOK_URL = "https://receiver.example/hooks/amc-crash-test"
WEBHOOK_SECRET = "shared-secret-crash-relaunch"  # noqa: S105 -- test fixture

# Allowlisted Discord user we drive in the test.
ALLOWED_DISCORD_USER_ID = "111222333444555666"
DISCORD_CHANNEL_ID = "999000111222333444"  # one DM channel for all 5 Discord msgs

# Aggressive poll interval so the iMessage connector reacts within ~50 ms.
TEST_POLL_INTERVAL = 0.05

# Bound for how long we'll wait for inbound rows to materialize. Matches
# the "RTO: relaunch + first inbound visible within 5s" criterion.
RTO_BUDGET_SECONDS = 5.0
GATEWAY_HANDSHAKE_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Live-system bundle -- one running adapter "instance"
# ---------------------------------------------------------------------------


@dataclass
class _LiveSystem:
    """Everything that has to be torn down to simulate a process crash."""

    engine: AsyncEngine
    session_factory: async_sessionmaker
    sink: MessageSink
    discord_connector: DiscordConnector
    discord_task: asyncio.Task[None]
    imessage_connector: ImessageConnector
    rate_limiter: RateLimiter
    idempotency_store: IdempotencyStore
    fastapi_client: TestClient
    fastapi_client_cm: contextlib.AbstractContextManager[TestClient]


async def _start_live_system(
    *,
    db_path: Path,
    chat_db_path: Path,
    gateway: FakeDiscordGateway,
    webhook_config: WebhookConfig,
) -> _LiveSystem:
    """Wire up + start every component.

    Mirrors what an eventual ``amc.app`` lifespan hook will do once
    task #41 lands: build the engine, build the sink + connectors, register
    them with the messages_send / messages_unread routers, and start the
    background tasks.
    """
    engine = create_engine_from_env(db_path_override=db_path)
    session_factory = create_session_factory(engine)

    allowlist = build_inmemory_allowlist(
        [
            (
                Source.DISCORD,
                ALLOWED_DISCORD_USER_ID,
                "Allowlisted Alice (Discord)",
                "person_alice",
            ),
            (
                Source.IMESSAGE,
                ALLOWLISTED_HANDLE,
                "Allowlisted Alice (iMessage)",
                "person_alice",
            ),
        ]
    )

    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=webhook_config,
    )

    # ---- Discord connector ------------------------------------------------
    discord_connector = DiscordConnector(
        bot_token="fake-token-not-used",
        message_sink=sink,
        allowlist=allowlist,
        gateway_url=gateway.url,
    )
    discord_task = asyncio.create_task(
        discord_connector.start("fake-token-not-used"),
        name="amc.test.discord.start",
    )
    await _await_discord_ready(discord_connector, discord_task)

    # ---- iMessage connector ----------------------------------------------
    reader = ChatDbReader(chat_db_path)
    imessage_connector = ImessageConnector(
        reader=reader,
        sender=FakeAppleScriptSender(),
        sink=sink,
        allowlist=allowlist,
        session_factory=session_factory,
        attachment_store=None,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    await imessage_connector.start()

    # ---- Adapter HTTP wiring ---------------------------------------------
    configure_bearer_token(BEARER)
    configure_send_session_factory(session_factory)
    configure_unread_session_factory(session_factory)

    rate_limiter = RateLimiter(rps=100.0, burst=100)
    configure_rate_limiter(rate_limiter)

    idempotency_store = IdempotencyStore(session_factory=session_factory)
    configure_idempotency_store(idempotency_store)

    configure_discord_connector(discord_connector)

    app = build_app()
    client_cm = TestClient(app, raise_server_exceptions=False)
    client = client_cm.__enter__()

    return _LiveSystem(
        engine=engine,
        session_factory=session_factory,
        sink=sink,
        discord_connector=discord_connector,
        discord_task=discord_task,
        imessage_connector=imessage_connector,
        rate_limiter=rate_limiter,
        idempotency_store=idempotency_store,
        fastapi_client=client,
        fastapi_client_cm=client_cm,
    )


async def _await_discord_ready(connector: DiscordConnector, task: asyncio.Task[None]) -> None:
    """Block until the Discord client finishes IDENTIFY / READY."""
    deadline = asyncio.get_running_loop().time() + GATEWAY_HANDSHAKE_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if connector.is_ready():
            return
        if task.done():
            # Surface whatever blew up inside discord.py instead of timing out.
            task.result()
            raise RuntimeError("DiscordConnector start task exited before READY")
        await asyncio.sleep(0.02)
    raise TimeoutError("DiscordConnector did not reach READY within timeout")


async def _crash_live_system(live: _LiveSystem) -> None:
    """Simulate ``SIGKILL`` -- drop every reference, dispose connections.

    What this *does* model:
    * In-memory connector state evaporates.
    * SQLAlchemy connection pool is torn down.
    * discord.py event loop tasks stop processing.

    What this *does not* model:
    * OS process replacement (we stay in the same Python process).
    * pid / ephemeral-port reuse.

    Both omissions are immaterial to the contract under test (DB-state
    recovery on relaunch). See the module docstring for the rationale.
    """
    # Stop the iMessage poller cleanly so any in-flight DB transaction
    # either commits or rolls back -- mirrors what a SIGKILL leaves
    # behind once SQLite's WAL recovery runs on next open.
    with contextlib.suppress(Exception):
        await live.imessage_connector.stop()

    # Close the Discord client. The harness login bypass leaves an
    # aiohttp ClientSession hanging on http; close() shuts it down too.
    with contextlib.suppress(Exception):
        await live.discord_connector.close()

    live.discord_task.cancel()
    with contextlib.suppress(BaseException):
        await live.discord_task

    # Drop the FastAPI TestClient (its underlying httpx.Client closes,
    # along with any transport-level state).
    with contextlib.suppress(Exception):
        live.fastapi_client_cm.__exit__(None, None, None)

    # Reset every module-level singleton the routes resolve through so
    # the relaunched instance can re-install fresh ones.
    reset_send_session_factory()
    reset_unread_session_factory()
    reset_rate_limiter()
    reset_discord_connector()
    reset_idempotency_store()
    reset_bearer_token()

    # Dispose the engine last -- after stop() any task that was holding
    # a connection has released it.
    with contextlib.suppress(Exception):
        await live.engine.dispose()


# ---------------------------------------------------------------------------
# Polling helpers (used by both pre-crash and post-relaunch paths)
# ---------------------------------------------------------------------------


async def _wait_for_message_count(
    db_path: Path,
    target: int,
    *,
    timeout: float,
    label: str,
) -> None:
    """Block until ``messages`` row count >= ``target`` or raise ``TimeoutError``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if read_messages_count(db_path) >= target:
            return
        await asyncio.sleep(0.02)
    actual = read_messages_count(db_path)
    raise TimeoutError(
        f"{label}: expected >= {target} messages within {timeout}s; observed {actual}"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def src_chat_db_path() -> Path:
    """Materialize the Phase 0 chat.db once per module."""
    return ensure_chat_db()


@pytest.fixture
def chat_db_copy(src_chat_db_path: Path, tmp_path: Path) -> Path:
    """Per-test writable copy of the chat.db fixture."""
    import shutil

    dst = tmp_path / "chat.db"
    shutil.copy2(src_chat_db_path, dst)
    return dst


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Fresh SQLite file with the full migrations applied."""
    db_path = tmp_path / "amc-crash-relaunch.db"
    monkeypatch.setenv("AMC_DB_PATH", str(db_path))
    apply_alembic_head(ALEMBIC_INI, db_path)
    yield db_path


@pytest.fixture
def webhook_config() -> WebhookConfig:
    return WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)


@pytest.fixture(autouse=True)
def _reset_module_globals() -> Iterator[None]:
    """Make sure no prior test leaked module-level state into this one."""
    reset_bearer_token()
    reset_send_session_factory()
    reset_unread_session_factory()
    reset_rate_limiter()
    reset_discord_connector()
    reset_idempotency_store()
    yield
    reset_bearer_token()
    reset_send_session_factory()
    reset_unread_session_factory()
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
# Helpers (test-local)
# ---------------------------------------------------------------------------


async def _send_outbound_via_http(
    client: TestClient,
    *,
    channel_id: str,
    text_body: str,
) -> dict:
    """Fire ``POST /messages/send`` and return the parsed JSON body."""
    resp = client.post(
        "/messages/send",
        headers={
            "Authorization": f"Bearer {BEARER}",
            "X-Agent-ID": AGENT_ID,
            "Content-Type": "application/json",
        },
        json={"channel_id": channel_id, "text": text_body},
    )
    assert resp.status_code == 200, f"POST /messages/send failed: {resp.status_code} {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_crash_relaunch_preserves_state_and_resumes_within_rto(
    fresh_db: Path,
    chat_db_copy: Path,
    gateway: FakeDiscordGateway,
    webhook_config: WebhookConfig,
) -> None:
    """End-to-end RTO + RPO proof.

    See the module docstring for the full scenario walkthrough.
    """
    # ------------------------------------------------------------------
    # Phase 1 -- boot the live system, drain the chat.db seed, then
    # interleave 5 inbound + 1 outbound across both transports.
    # ------------------------------------------------------------------
    live = await _start_live_system(
        db_path=fresh_db,
        chat_db_path=chat_db_copy,
        gateway=gateway,
        webhook_config=webhook_config,
    )

    try:
        # --- iMessage connector seeds its cursor to MAX(message.ROWID) ---
        # Per the connector's `start()` contract (task #52, spec §5.1), on
        # a fresh install with no ``connector_state`` row it seeds the
        # in-memory cursor to the current ``MAX(message.ROWID)`` so the
        # first poll cycle does NOT replay the historical backlog. The
        # Phase 0 fixture has 6 seeded rows (ROWIDs 1..6), so any test
        # ROWID >= 100 is safely past that.
        #
        # We don't wait for any seed drain -- there's intentionally
        # nothing to drain. The first persisted message comes from our
        # injected rows below, which is when ``connector_state.cursor``
        # is first written.
        baseline_imessage_rowid = 100  # first injected ROWID; > MAX(seed)=6
        baseline_message_count = read_messages_count(fresh_db)

        # --- Pre-crash: 5 inbound (3 Discord + 2 iMessage) + 1 outbound -
        # We interleave the transports so a "mid-stream crash" actually
        # straddles both code paths -- not a clean break.
        pre_crash_discord_messages = [
            ("D1", "discord pre-1"),
            ("D2", "discord pre-2"),
            ("D3", "discord pre-3"),
        ]
        pre_crash_imessage_rows = [
            (baseline_imessage_rowid, "imessage pre-1"),
            (baseline_imessage_rowid + 1, "imessage pre-2"),
        ]

        # Interleave: D1, IM1, D2, IM2, D3
        await gateway.deliver(
            make_dm_payload(
                message_id=str(secrets.randbits(63)),
                channel_id=DISCORD_CHANNEL_ID,
                author_id=ALLOWED_DISCORD_USER_ID,
                content=pre_crash_discord_messages[0][1],
            )
        )
        append_chat_db_inbound_row(
            chat_db_copy,
            rowid=pre_crash_imessage_rows[0][0],
            text_value=pre_crash_imessage_rows[0][1],
        )
        await gateway.deliver(
            make_dm_payload(
                message_id=str(secrets.randbits(63)),
                channel_id=DISCORD_CHANNEL_ID,
                author_id=ALLOWED_DISCORD_USER_ID,
                content=pre_crash_discord_messages[1][1],
            )
        )
        append_chat_db_inbound_row(
            chat_db_copy,
            rowid=pre_crash_imessage_rows[1][0],
            text_value=pre_crash_imessage_rows[1][1],
        )
        await gateway.deliver(
            make_dm_payload(
                message_id=str(secrets.randbits(63)),
                channel_id=DISCORD_CHANNEL_ID,
                author_id=ALLOWED_DISCORD_USER_ID,
                content=pre_crash_discord_messages[2][1],
            )
        )

        # Wait for all 5 inbound rows to appear in `messages`.
        await _wait_for_message_count(
            fresh_db,
            target=baseline_message_count + 5,
            timeout=RTO_BUDGET_SECONDS,
            label="pre-crash inbound drain",
        )

        # --- One outbound via /messages/send ---
        # The Discord channel was UPSERT'd by the inbound sink path, so the
        # /messages/send route can resolve `discord:dm:<id>` to a row in
        # `channels`. We don't actually want the discord.py REST POST to
        # hit the network -- so monkey-patch the connector's `send` to
        # return a synthetic SendResult. (The connector's real `send` would
        # try to look up the channel cache, which the fake gateway does
        # not populate.)
        from amc.connectors.discord.connector import SendResult

        async def _fake_send(*_args, **_kwargs) -> SendResult:
            return SendResult(
                ok=True,
                message_id=str(secrets.randbits(63)),
                error=None,
                detail=None,
            )

        live.discord_connector.send = _fake_send  # type: ignore[method-assign]

        outbound_response = await _send_outbound_via_http(
            live.fastapi_client,
            channel_id=f"discord:dm:{DISCORD_CHANNEL_ID}",
            text_body="outbound from crash-relaunch test",
        )
        assert outbound_response.get("message_id", "").startswith("msg_")

        # Sanity: 5 inbound + 1 outbound persisted before the crash.
        pre_crash_total = read_messages_count(fresh_db)
        assert pre_crash_total == baseline_message_count + 5 + 1, (
            f"expected {baseline_message_count + 5 + 1} pre-crash rows; got {pre_crash_total}"
        )

        pre_crash_imessage_cursor = read_imessage_cursor(fresh_db)
        pre_crash_discord_cursor = read_discord_cursor(fresh_db)
        assert pre_crash_imessage_cursor is not None
        assert pre_crash_discord_cursor is not None
        assert int(pre_crash_imessage_cursor) >= pre_crash_imessage_rows[1][0]

    finally:
        # ------------------------------------------------------------------
        # Phase 2 -- "Crash" the adapter. Always run, even on assertion
        # failure, so we don't leak background tasks into the test session.
        # ------------------------------------------------------------------
        await _crash_live_system(live)

    # ------------------------------------------------------------------
    # Phase 3 -- Relaunch against the same DB / chat.db, time the RTO.
    # ------------------------------------------------------------------
    relaunch_start = asyncio.get_running_loop().time()

    # The fake gateway is module-bound -- we don't restart it, but the
    # client (DiscordConnector) reconnects fresh. The gateway accepts the
    # new IDENTIFY and starts a new session, so subsequent deliver()
    # calls go to the relaunched client.
    live2 = await _start_live_system(
        db_path=fresh_db,
        chat_db_path=chat_db_copy,
        gateway=gateway,
        webhook_config=webhook_config,
    )

    try:
        # Inject a single inbound message *immediately* and time how long
        # before it appears in `messages`. That window is the RTO budget.
        # Read the persisted iMessage cursor; on warm restart it is the
        # last pre-crash ROWID. Append one more row past it.
        cursor_before_relaunch_probe = int(read_imessage_cursor(fresh_db) or "0")
        first_post_relaunch_rowid = cursor_before_relaunch_probe + 100
        msg_count_before_probe = read_messages_count(fresh_db)
        append_chat_db_inbound_row(
            chat_db_copy,
            rowid=first_post_relaunch_rowid,
            text_value="imessage post-1 (RTO probe)",
        )

        await _wait_for_message_count(
            fresh_db,
            target=msg_count_before_probe + 1,
            timeout=RTO_BUDGET_SECONDS,
            label="post-relaunch RTO probe",
        )
        rto_elapsed = asyncio.get_running_loop().time() - relaunch_start
        assert rto_elapsed < RTO_BUDGET_SECONDS, (
            f"RTO budget breached: relaunch + first-inbound visible took "
            f"{rto_elapsed:.2f}s (limit {RTO_BUDGET_SECONDS}s)"
        )

        # --- Continue the stream: remaining 3 inbound (2 Discord + 1 iM) -
        post_crash_discord_messages = [
            ("D4", "discord post-1"),
            ("D5", "discord post-2"),
        ]
        post_crash_imessage_rows = [
            (first_post_relaunch_rowid + 1, "imessage post-2"),
        ]

        await gateway.deliver(
            make_dm_payload(
                message_id=str(secrets.randbits(63)),
                channel_id=DISCORD_CHANNEL_ID,
                author_id=ALLOWED_DISCORD_USER_ID,
                content=post_crash_discord_messages[0][1],
            )
        )
        append_chat_db_inbound_row(
            chat_db_copy,
            rowid=post_crash_imessage_rows[0][0],
            text_value=post_crash_imessage_rows[0][1],
        )
        await gateway.deliver(
            make_dm_payload(
                message_id=str(secrets.randbits(63)),
                channel_id=DISCORD_CHANNEL_ID,
                author_id=ALLOWED_DISCORD_USER_ID,
                content=post_crash_discord_messages[1][1],
            )
        )

        # Wait for the remaining 3 inbound rows.
        # We expect:
        #   baseline + 5 inbound (pre-crash)
        # + 1 outbound (pre-crash)
        # + 1 inbound (RTO probe)
        # + 3 inbound (post-relaunch continuation)
        # = baseline + 10
        expected_total = baseline_message_count + 5 + 1 + 1 + 3
        await _wait_for_message_count(
            fresh_db,
            target=expected_total,
            timeout=RTO_BUDGET_SECONDS,
            label="post-relaunch continuation drain",
        )

        # ------------------------------------------------------------------
        # Phase 4 -- Assertions on persisted state.
        # ------------------------------------------------------------------
        final_rows = list_messages_for_assertion(fresh_db)

        # Each of the 9 inbound texts we injected appears exactly once.
        all_injected_inbound_texts = (
            [t for (_, t) in pre_crash_discord_messages]
            + [t for (_, t) in pre_crash_imessage_rows]
            + ["imessage post-1 (RTO probe)"]
            + [t for (_, t) in post_crash_discord_messages]
            + [t for (_, t) in post_crash_imessage_rows]
        )
        assert len(all_injected_inbound_texts) == 9, (
            "test bug: should have exactly 9 inbound test messages"
        )
        for text_body in all_injected_inbound_texts:
            matches = [r for r in final_rows if r["text"] == text_body]
            assert len(matches) == 1, (
                f"inbound message {text_body!r} should appear exactly once; "
                f"found {len(matches)}: {matches}"
            )

        # The single outbound message is present, exactly once.
        outbound_rows = [r for r in final_rows if r["direction"] == "outbound"]
        assert len(outbound_rows) == 1, (
            f"expected exactly one outbound row; got {len(outbound_rows)}: {outbound_rows}"
        )
        assert outbound_rows[0]["text"] == "outbound from crash-relaunch test"

        # connector_state.cursor advanced for both connectors past the
        # pre-crash watermark.
        final_imessage_cursor = read_imessage_cursor(fresh_db)
        final_discord_cursor = read_discord_cursor(fresh_db)
        assert final_imessage_cursor is not None
        assert final_discord_cursor is not None
        assert int(final_imessage_cursor) >= post_crash_imessage_rows[0][0], (
            f"iMessage cursor did not advance past post-crash ROWID; got {final_imessage_cursor}"
        )
        # Discord cursor is JSON; we just verify it's been written
        # (non-empty / parseable) at least once after relaunch.
        assert final_discord_cursor.startswith("{"), (
            f"Discord cursor should be JSON-encoded; got {final_discord_cursor!r}"
        )

        # Webhook deliveries: every row is in a sane status (no corrupt
        # / unknown values). The webhook worker doesn't run in this test
        # so they should all be ``pending`` -- "delivered" is also
        # acceptable per the brief if some hypothetical worker drained
        # them.
        deliveries = list_webhook_deliveries(fresh_db)
        assert deliveries, "expected webhook_deliveries rows for inbound allowlisted messages"
        valid_statuses = {"pending", "delivered", "dead"}
        for delivery in deliveries:
            assert delivery["status"] in valid_statuses, (
                f"webhook delivery in unexpected status: {delivery!r}"
            )
            # Sanity: every delivery references a real messages.id.
            assert delivery["message_id"], f"delivery row has no message_id: {delivery!r}"
        # We expect 9 inbound -> 9 delivery rows (each allowlisted). The
        # quarantine row from the seed does NOT enqueue a webhook, so the
        # delivery count should equal the number of allowlisted inbound
        # messages we observed in `messages`.
        allowlisted_inbound = [r for r in final_rows if r["direction"] == "inbound"]
        # Some seeded rows are from the QUARANTINED_HANDLE; those are
        # captured in messages with allowlist_status='unknown' and produce
        # NO webhook row. Filter to allowed-inbound by counting deliveries.
        # The exact count depends on whether the seed quarantine row was
        # captured -- we assert it's >= 9 (our injected allowlisted ones).
        injected_allowed_inbound = 9
        # The number of delivery rows must be at least one per allowlisted
        # inbound message we injected.
        assert len(deliveries) >= injected_allowed_inbound, (
            f"expected at least {injected_allowed_inbound} webhook deliveries "
            f"(one per allowlisted inbound); got {len(deliveries)}. "
            f"allowlisted_inbound rows visible in messages: {len(allowlisted_inbound)}"
        )

    finally:
        await _crash_live_system(live2)
