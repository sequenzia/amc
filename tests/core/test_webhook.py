"""Tests for the outbound webhook delivery worker (``amg.core.webhook``).

Covers spec §5.5 (acceptance criteria), §6.4 (retry policy), §7.4.11
(header table), and OQ-6 (refuse-to-start when URL set without secret).

Strategy
--------
* DB: a tmp_path SQLite file produced by running ``alembic upgrade head``
  via the programmatic API (same pattern as ``tests/core/test_schema.py``).
* HTTP: ``respx`` intercepts the ``httpx.AsyncClient`` so we can assert on
  request bodies, headers, and the HMAC signature.
* Time: an injectable ``time_provider`` lets us "fast-forward" through the
  10-minute backoff without sleeping.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from alembic import command
from alembic.config import Config

from amg.core.db import build_database_url, create_engine_from_env, create_session_factory
from amg.core.webhook import (
    BACKOFF_SECONDS,
    ENV_WEBHOOK_SECRET,
    ENV_WEBHOOK_URL,
    MAX_ATTEMPTS,
    WebhookConfig,
    WebhookConfigError,
    WebhookWorker,
    compute_signature,
    enqueue_webhook,
    load_webhook_config_from_env,
    validate_webhook_config_or_exit,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

WEBHOOK_URL = "https://receiver.example/hooks/amg"
WEBHOOK_SECRET = "shared-secret-1234567890"  # noqa: S105 - test fixture


class _FakeClock:
    """Manually-advanced wall-clock for deterministic time_provider tests."""

    __slots__ = ("now",)

    def __init__(self, start: datetime | None = None) -> None:
        # Far-future default so tests stay hermetic against wall-clock drift:
        # `enqueue_webhook` defaults to real `datetime.now(UTC)` when callers
        # don't thread a `time_provider`, and the worker's tick filters
        # `next_retry_at <= fake_now`. As long as the fake clock is later
        # than wall time, that comparison holds.
        self.now = start or datetime(2099, 1, 1, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Tmp_path SQLite file with the full schema applied via alembic."""
    db_path = tmp_path / "webhook.db"
    monkeypatch.setenv("AMG_DB_PATH", str(db_path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    yield db_path


@pytest.fixture
def session_factory(fresh_db: Path):
    """Async session factory bound to the fresh DB. Disposes engine on teardown.

    We dispose the engine via ``asyncio.run`` (rather than the test's running
    loop) because the fixture teardown can run after pytest-asyncio has
    closed its loop. ``aiosqlite`` is happy to dispose from a fresh loop.
    """
    engine = create_engine_from_env(db_path_override=fresh_db)
    factory = create_session_factory(engine)
    yield factory
    asyncio.run(engine.dispose())


def _seed_message(
    db_path: Path,
    *,
    message_id: str = "msg_01HXYZ1234567890ABCDEFGHJK",
    text_body: str = "hello, world",
    sender_id: str = "+15551234567",
    channel_id: str = "+15551234567",
    sender_display: str = "Alice",
    sender_person_id: str = "alice",
    timestamp: str = "2026-05-03T12:00:00Z",
    attachments_json: str | None = None,
) -> None:
    """Insert one channel + sender + message row so the worker can reload it."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) "
            "VALUES ('imessage', :channel_id, 'dm')",
            {"channel_id": channel_id},
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status, person_id) "
            "VALUES ('imessage', :sender_id, :display_name, 'allowed', :person_id)",
            {
                "sender_id": sender_id,
                "display_name": sender_display,
                "person_id": sender_person_id,
            },
        )
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts, attachments_json) "
            "VALUES (:id, 'imessage', :channel_id, 'dm', :sender_id, :text, "
            "        'inbound', 'allowed', :ts, :attachments_json)",
            {
                "id": message_id,
                "channel_id": channel_id,
                "sender_id": sender_id,
                "text": text_body,
                "ts": timestamp,
                "attachments_json": attachments_json,
            },
        )
        conn.commit()
    finally:
        conn.close()


def _read_delivery_row(db_path: Path, delivery_id: str) -> dict:
    """Return the named delivery row as a dict (or raise if not found)."""
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


def _list_deliveries(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM webhook_deliveries ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Config + OQ-6
# ---------------------------------------------------------------------------


def test_config_loads_when_url_and_secret_present():
    config = load_webhook_config_from_env(
        {ENV_WEBHOOK_URL: WEBHOOK_URL, ENV_WEBHOOK_SECRET: WEBHOOK_SECRET}
    )
    assert config == WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)


def test_config_returns_none_when_url_unset():
    assert load_webhook_config_from_env({}) is None
    assert load_webhook_config_from_env({ENV_WEBHOOK_URL: ""}) is None


def test_config_returns_none_when_url_unset_secret_set():
    """An unset URL disables the worker even if a secret is present."""
    assert load_webhook_config_from_env({ENV_WEBHOOK_SECRET: WEBHOOK_SECRET}) is None


def test_config_refuses_when_url_set_secret_missing_OQ6():
    """OQ-6: URL set without secret → adapter refuses to start."""
    with pytest.raises(WebhookConfigError, match="AMG_WEBHOOK_SECRET"):
        load_webhook_config_from_env({ENV_WEBHOOK_URL: WEBHOOK_URL})


def test_config_refuses_when_url_set_secret_empty_OQ6():
    with pytest.raises(WebhookConfigError):
        load_webhook_config_from_env({ENV_WEBHOOK_URL: WEBHOOK_URL, ENV_WEBHOOK_SECRET: ""})


def test_validate_webhook_config_or_exit_passes_through_valid_config():
    config = validate_webhook_config_or_exit(
        {ENV_WEBHOOK_URL: WEBHOOK_URL, ENV_WEBHOOK_SECRET: WEBHOOK_SECRET}
    )
    assert config is not None
    assert config.url == WEBHOOK_URL


def test_validate_webhook_config_or_exit_returns_none_when_disabled():
    assert validate_webhook_config_or_exit({}) is None


def test_validate_webhook_config_or_exit_raises_on_oq6_violation():
    with pytest.raises(WebhookConfigError):
        validate_webhook_config_or_exit({ENV_WEBHOOK_URL: WEBHOOK_URL})


# ---------------------------------------------------------------------------
# HMAC
# ---------------------------------------------------------------------------


def test_compute_signature_golden():
    """HMAC-SHA256 over raw bytes matches a hand-computed expected value."""
    body = b'{"hello":"world"}'
    expected_hex = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert compute_signature(WEBHOOK_SECRET, body) == f"sha256={expected_hex}"


def test_compute_signature_rejects_string_input():
    """Signing a `str` instead of bytes is a programmer error — fail fast."""
    with pytest.raises(TypeError, match="bytes"):
        compute_signature(WEBHOOK_SECRET, "not bytes")  # type: ignore[arg-type]


def test_compute_signature_is_deterministic():
    body = b"payload"
    sig1 = compute_signature(WEBHOOK_SECRET, body)
    sig2 = compute_signature(WEBHOOK_SECRET, body)
    assert sig1 == sig2


def test_compute_signature_changes_with_secret():
    body = b"payload"
    assert compute_signature("a", body) != compute_signature("b", body)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def test_enqueue_disabled_returns_none_and_creates_no_row(session_factory, fresh_db: Path):
    """`AMG_WEBHOOK_URL` unset → no rows produced (spec §5.5)."""
    async with session_factory() as session:
        result = await enqueue_webhook(session, "msg_x", config=None)
        await session.commit()
    assert result is None
    assert _list_deliveries(fresh_db) == []


async def test_enqueue_inserts_pending_row_and_records_url_snapshot(
    session_factory, fresh_db: Path
):
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    snapshot: dict[str, str] = {}
    _seed_message(fresh_db)
    async with session_factory() as session:
        delivery_id = await enqueue_webhook(
            session,
            "msg_01HXYZ1234567890ABCDEFGHJK",
            config=config,
            url_snapshot=snapshot,
        )
        await session.commit()
    assert delivery_id is not None
    rows = _list_deliveries(fresh_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == delivery_id
    assert row["status"] == "pending"
    assert row["attempt"] == 0
    assert row["next_retry_at"] is not None
    # URL snapshot recorded so later config changes don't re-target retries.
    assert snapshot == {delivery_id: WEBHOOK_URL}


# ---------------------------------------------------------------------------
# Worker — happy path (2xx)
# ---------------------------------------------------------------------------


async def test_tick_delivers_2xx_and_records_headers_signature(session_factory, fresh_db: Path):
    """A 200 response marks the row delivered; required headers are present."""
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    captured_request: dict = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured_request["headers"] = dict(request.headers)
        captured_request["body"] = request.content
        captured_request["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=True) as router:
        router.post(WEBHOOK_URL).mock(side_effect=_record)
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            async with session_factory() as session:
                delivery_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ1234567890ABCDEFGHJK",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            processed = await worker.tick()

    assert processed == 1
    assert delivery_id is not None

    # Headers (spec §7.4.11)
    headers = captured_request["headers"]
    assert headers["content-type"] == "application/json"
    assert headers["x-amg-delivery-id"] == delivery_id
    assert headers["x-amg-message-id"] == "msg_01HXYZ1234567890ABCDEFGHJK"
    assert headers["x-amg-attempt"] == "1"

    # Signature: HMAC over the EXACT bytes the receiver sees.
    body_bytes = captured_request["body"]
    expected_sig = compute_signature(WEBHOOK_SECRET, body_bytes)
    assert headers["x-amg-signature"] == expected_sig

    # Body is the envelope JSON; round-trip parse.
    payload = json.loads(body_bytes)
    assert payload["id"] == "msg_01HXYZ1234567890ABCDEFGHJK"
    assert payload["source"] == "imessage"
    assert payload["sender"]["id"] == "+15551234567"

    # Row marked delivered.
    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "delivered"
    assert row["next_retry_at"] is None
    assert row["last_response_code"] == 200
    assert row["attempt"] == 1


async def test_tick_returns_zero_when_disabled(session_factory):
    worker = WebhookWorker(session_factory=session_factory, config=None)
    assert await worker.tick() == 0


# ---------------------------------------------------------------------------
# Worker — 4xx dead-letter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_tick_4xx_dead_letters_immediately(session_factory, fresh_db: Path, status: int):
    """Any 4xx → dead immediately, no retry (spec §5.5)."""
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    with respx.mock() as router:
        route = router.post(WEBHOOK_URL).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            async with session_factory() as session:
                delivery_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ1234567890ABCDEFGHJK",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            processed = await worker.tick()

    assert processed == 1
    assert route.call_count == 1
    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "dead"
    assert row["next_retry_at"] is None
    assert row["last_response_code"] == status


# ---------------------------------------------------------------------------
# Worker — 5xx retry + backoff schedule
# ---------------------------------------------------------------------------


async def test_tick_5xx_schedules_retry_with_first_backoff(session_factory, fresh_db: Path):
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    with respx.mock() as router:
        router.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            async with session_factory() as session:
                delivery_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ1234567890ABCDEFGHJK",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            await worker.tick()

    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "pending"
    assert row["attempt"] == 1
    # The worker emits ISO with 6-digit microseconds + "Z" so string sort
    # mirrors chronological order (see ``_to_iso_z``). Match that format.
    expected_next = clock() + timedelta(seconds=BACKOFF_SECONDS[0])
    expected_iso = expected_next.isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert row["next_retry_at"] == expected_iso
    assert row["last_response_code"] == 500


async def test_full_backoff_schedule_dead_letters_after_5_attempts(session_factory, fresh_db: Path):
    """After 5 failed attempts the row is dead; honor the [1,5,30,120,600] schedule.

    Walk through every attempt by fast-forwarding the clock past each
    scheduled ``next_retry_at`` and re-running ``tick``. Asserts:

    - exactly 5 POSTs are issued
    - schedule matches BACKOFF_SECONDS for attempts 1..4
    - attempt 5 fails → status='dead', next_retry_at is NULL
    """
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    with respx.mock() as router:
        route = router.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            async with session_factory() as session:
                delivery_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ1234567890ABCDEFGHJK",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            # Attempt 1 — immediate.
            await worker.tick()
            row = _read_delivery_row(fresh_db, delivery_id)
            assert row["attempt"] == 1
            assert row["status"] == "pending"

            # Attempts 2..5 — fast-forward through each backoff slot.
            for attempt_index in range(MAX_ATTEMPTS - 1):  # 0..3 -> attempts 2..5
                clock.advance(BACKOFF_SECONDS[attempt_index] + 0.001)
                await worker.tick()

            assert route.call_count == MAX_ATTEMPTS

    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "dead"
    assert row["attempt"] == MAX_ATTEMPTS
    assert row["next_retry_at"] is None
    assert row["last_response_code"] == 500


async def test_tick_skips_rows_whose_next_retry_at_is_in_the_future(
    session_factory, fresh_db: Path
):
    """A retry must not fire before its scheduled ``next_retry_at``."""
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    with respx.mock() as router:
        route = router.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            async with session_factory() as session:
                delivery_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ1234567890ABCDEFGHJK",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            await worker.tick()  # attempt 1 fails, schedule +1s
            assert route.call_count == 1

            # Don't advance the clock — the retry must NOT fire.
            await worker.tick()
            assert route.call_count == 1

            # Now advance and retry should fire.
            clock.advance(BACKOFF_SECONDS[0] + 0.001)
            await worker.tick()
            assert route.call_count == 2

    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["attempt"] == 2
    assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# Worker — network error / timeout
# ---------------------------------------------------------------------------


async def test_tick_network_error_treated_as_5xx_and_retried(session_factory, fresh_db: Path):
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    with respx.mock() as router:
        router.post(WEBHOOK_URL).mock(side_effect=httpx.ConnectError("could not connect"))
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            async with session_factory() as session:
                delivery_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ1234567890ABCDEFGHJK",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            await worker.tick()

    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "pending"
    assert row["attempt"] == 1
    assert row["last_response_code"] is None
    assert "network_error" in (row["last_error"] or "")


async def test_tick_timeout_treated_as_5xx_and_retried(session_factory, fresh_db: Path):
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    with respx.mock() as router:
        router.post(WEBHOOK_URL).mock(side_effect=httpx.ReadTimeout("read timed out"))
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            async with session_factory() as session:
                delivery_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ1234567890ABCDEFGHJK",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            await worker.tick()

    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "pending"
    assert row["attempt"] == 1
    assert "timeout" in (row["last_error"] or "")


# ---------------------------------------------------------------------------
# URL captured at queue time
# ---------------------------------------------------------------------------


async def test_in_flight_retry_uses_url_captured_at_queue_time(session_factory, fresh_db: Path):
    """Spec §5.5 edge case: changing the live config does not re-target retries.

    Enqueue with URL_A; before tick fires, mutate the worker's *config* URL
    to URL_B. The POST should still go to URL_A because the per-delivery
    snapshot is the source of truth.
    """
    url_a = "https://receiver-a.example/hook"
    url_b = "https://receiver-b.example/hook"
    config_a = WebhookConfig(url=url_a, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    with respx.mock(assert_all_called=False) as router:
        route_a = router.post(url_a).mock(return_value=httpx.Response(200))
        route_b = router.post(url_b).mock(return_value=httpx.Response(200))

        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config_a,
                time_provider=clock,
                http_client=client,
            )
            async with session_factory() as session:
                await enqueue_webhook(
                    session,
                    "msg_01HXYZ1234567890ABCDEFGHJK",
                    config=config_a,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            # Simulate config reload to URL_B (operator SIGHUP).
            worker._config = WebhookConfig(url=url_b, secret=WEBHOOK_SECRET)

            await worker.tick()

    assert route_a.call_count == 1
    assert route_b.call_count == 0


# ---------------------------------------------------------------------------
# Restart survival
# ---------------------------------------------------------------------------


async def test_worker_picks_up_pending_rows_after_restart(session_factory, fresh_db: Path):
    """A row enqueued by a previous worker is delivered by a fresh worker.

    Simulates: process A enqueues, dies before delivering. Process B starts,
    sees the pending row in the DB, picks it up, delivers. The fresh worker
    has an empty in-memory snapshot, so it falls back to its live config URL
    — which matches operator intent ("post-restart, use the new config").
    """
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    # --- Process A: enqueue, do not deliver ---
    snapshot_a: dict[str, str] = {}
    async with session_factory() as session:
        delivery_id = await enqueue_webhook(
            session,
            "msg_01HXYZ1234567890ABCDEFGHJK",
            config=config,
            url_snapshot=snapshot_a,
        )
        await session.commit()
    assert delivery_id is not None
    # Confirm it's pending in the DB.
    assert _read_delivery_row(fresh_db, delivery_id)["status"] == "pending"

    # --- Process B: fresh worker, fresh in-memory snapshot ---
    with respx.mock() as router:
        route = router.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            worker_b = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            assert worker_b.url_snapshot == {}  # empty after restart

            processed = await worker_b.tick()
            assert processed == 1
            assert route.call_count == 1

    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "delivered"
    assert row["last_response_code"] == 200


# ---------------------------------------------------------------------------
# start() / stop() lifecycle
# ---------------------------------------------------------------------------


async def test_start_is_noop_when_disabled(session_factory):
    worker = WebhookWorker(session_factory=session_factory, config=None)
    await worker.start()
    assert worker._task is None  # never spawned
    await worker.stop()


async def test_start_spawns_task_then_stop_cancels_it(session_factory, fresh_db: Path):
    """``start`` creates the loop task; ``stop`` cancels and joins it cleanly."""
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()

    with respx.mock(assert_all_called=False) as router:
        router.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        worker = WebhookWorker(
            session_factory=session_factory,
            config=config,
            time_provider=clock,
            idle_poll_seconds=0.01,
        )
        await worker.start()
        # Yield once so the loop body runs at least once.
        await asyncio.sleep(0.05)
        assert worker._task is not None
        assert not worker._task.done() or worker._task.cancelled()
        await worker.stop()
        assert worker._task is None


# ---------------------------------------------------------------------------
# Misc — ordering and message-missing
# ---------------------------------------------------------------------------


async def test_tick_processes_rows_in_next_retry_at_ascending_order(
    session_factory, fresh_db: Path
):
    """Multiple pending rows process oldest-first."""
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    # Seed two messages.
    _seed_message(
        fresh_db,
        message_id="msg_01HXYZ1111111111111111111A",
        sender_id="+15551234567",
        channel_id="+15551234567",
    )
    # Add a second message (different sender/channel to avoid PK collisions).
    conn = sqlite3.connect(fresh_db)
    try:
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) "
            "VALUES ('imessage', '+15559999999', 'dm')"
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES ('imessage', '+15559999999', 'Bob', 'allowed')"
        )
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts) "
            "VALUES ('msg_01HXYZ2222222222222222222B', 'imessage', '+15559999999', "
            "        'dm', '+15559999999', 'second', 'inbound', 'allowed', "
            "        '2026-05-03T12:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    seen_order: list[str] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen_order.append(request.headers["x-amg-message-id"])
        return httpx.Response(200)

    with respx.mock() as router:
        router.post(WEBHOOK_URL).mock(side_effect=_record)
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            # Enqueue first message at t0.
            async with session_factory() as session:
                first_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ1111111111111111111A",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            # Advance the clock then enqueue the second so its next_retry_at is later.
            clock.advance(5.0)
            async with session_factory() as session:
                second_id = await enqueue_webhook(
                    session,
                    "msg_01HXYZ2222222222222222222B",
                    config=config,
                    url_snapshot=worker.url_snapshot,
                )
                await session.commit()

            # Both ready now.
            await worker.tick()

    assert first_id is not None
    assert second_id is not None
    assert seen_order == [
        "msg_01HXYZ1111111111111111111A",
        "msg_01HXYZ2222222222222222222B",
    ]


async def test_tick_marks_dead_when_message_row_is_missing(session_factory, fresh_db: Path):
    """If the message row referenced by a pending delivery was lost, dead-letter.

    We can't actually delete a ``messages`` row that has a FK from
    ``webhook_deliveries`` without temporarily disabling FK enforcement,
    so the test inserts a delivery row that points at a non-existent
    ``message_id`` directly (simulating a row that survived a manual
    operator intervention or a future schema drift).
    """
    config = WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    clock = _FakeClock()
    _seed_message(fresh_db)

    # Insert a delivery row whose message_id was deleted out from under us.
    # Bypass FKs via a direct sqlite3 connection.
    bogus_msg = "msg_01HXYZ9999999999999999999Z"
    delivery_id = "01HXYZ_FAKE_DELIVERY_ID_AAAAA"
    conn = sqlite3.connect(fresh_db)
    try:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute(
            "INSERT INTO webhook_deliveries "
            "(id, message_id, attempt, next_retry_at, status, created_at, updated_at) "
            "VALUES (?, ?, 0, ?, 'pending', ?, ?)",
            (
                delivery_id,
                bogus_msg,
                "2026-05-03T12:00:00.000000Z",
                "2026-05-03T12:00:00.000000Z",
                "2026-05-03T12:00:00.000000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with respx.mock(assert_all_called=False) as router:
        route = router.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            worker = WebhookWorker(
                session_factory=session_factory,
                config=config,
                time_provider=clock,
                http_client=client,
            )
            await worker.tick()

    assert route.call_count == 0
    row = _read_delivery_row(fresh_db, delivery_id)
    assert row["status"] == "dead"


# ---------------------------------------------------------------------------
# Sanity: build_database_url helper used by the engine factory
# ---------------------------------------------------------------------------


def test_build_database_url_imported(fresh_db: Path):
    """Smoke check that the test fixture wires through the same helpers the
    runtime uses (catch a regression where one path drifts from the other)."""
    url = build_database_url(fresh_db)
    assert url.startswith("sqlite+aiosqlite:///")
