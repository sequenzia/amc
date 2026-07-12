"""Tests for ``amg.connectors.imessage.connector.ImessageConnector``.

The connector is the inbound poller + outbound AppleScript driver for
the iMessage path (spec §5.1, §5.2, §7.5). These tests exercise it
against the Phase 0 fixture chat.db (built by
``tests/fixtures/build_chat_db.py``), the
:class:`tests.fakes.applescript.FakeAppleScriptSender`, an in-memory
:class:`amg.core.allowlist.AllowlistLoader`, and the real
:class:`amg.core.message_sink.MessageSink` against a tmp-path SQLite.

Coverage map (acceptance criterion -> test):

Functional
- Poller picks up new rows within 1s
    -> ``test_poller_picks_up_appended_row_within_one_cycle``
- Each row produces an envelope through MessageSink
    -> ``test_each_row_produces_envelope_through_sink``
- ``is_from_me=1`` rows are skipped
    -> ``test_is_from_me_rows_skipped``
- Group chats are skipped (DM only via reader filter)
    -> ``test_group_chat_rows_skipped``
- Cursor advances transactionally (and persists across restart)
    -> ``test_cursor_persisted_in_connector_state_after_poll``
    -> ``test_connector_restart_resumes_from_cursor``
- If MessageSink raises, cursor does NOT advance for that row
    -> ``test_sink_failure_does_not_advance_cursor``
- AppleScript send failure -> PLATFORM_SEND_FAILED
    -> ``test_send_failure_returns_platform_send_failed``
- Send routes to the metadata_json chat.guid for known channels
    -> ``test_send_uses_metadata_json_chat_guid``
- Send falls back to ``iMessage;-;<channel_id>`` when no metadata exists
    -> ``test_send_falls_back_to_canonical_chat_guid``

Error handling
- chat.db missing at startup -> degraded; logs ERROR
    -> ``test_missing_chat_db_marks_degraded_and_logs``
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker

from amg.connectors.imessage.applescript import SendResult
from amg.connectors.imessage.connector import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    ROWID_GAP_WARN_THRESHOLD,
    ImessageConnector,
)
from amg.connectors.imessage.reader import ChatDbPathMissingError, ChatDbReader
from amg.core.allowlist import AllowlistEntry, AllowlistLoader
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.envelope import (
    AllowlistStatus,
    ChannelType,
    Direction,
    Envelope,
    Source,
)
from amg.core.message_sink import MessageSink
from tests.fakes.applescript import FakeAppleScriptSender
from tests.fixtures.build_chat_db import (
    ALLOWLISTED_HANDLE,
    CHAT_GUID,
    QUARANTINED_HANDLE,
    ensure_chat_db,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Aggressive poll interval so tests never wait a full real second.
TEST_POLL_INTERVAL = 0.05


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def src_chat_db_path() -> Path:
    """Materialize the Phase 0 fixture chat.db once per session."""
    return ensure_chat_db()


@pytest.fixture
def chat_db_copy(src_chat_db_path: Path, tmp_path: Path) -> Path:
    """Per-test writable copy of the fixture chat.db."""
    dst = tmp_path / "chat.db"
    shutil.copy2(src_chat_db_path, dst)
    return dst


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """tmp_path SQLite file with the full schema applied via alembic."""
    db_path = tmp_path / "amg.db"
    monkeypatch.setenv("AMG_DB_PATH", str(db_path))
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
        import contextlib

        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture
def allowlist() -> AllowlistLoader:
    """Loader with the fixture's allowlisted handle pre-installed.

    Bypasses the TOML parse — we set ``_entries`` directly. The class's
    ``__slots__`` contract still holds (``path`` + ``_entries``).
    """
    loader = AllowlistLoader.__new__(AllowlistLoader)
    loader.path = None  # type: ignore[assignment]
    loader._entries = {
        (Source.IMESSAGE, ALLOWLISTED_HANDLE): AllowlistEntry(
            source=Source.IMESSAGE,
            id=ALLOWLISTED_HANDLE,
            display_name="Alice",
            person_id="alice",
        ),
    }
    return loader


@pytest.fixture
def fake_sender() -> FakeAppleScriptSender:
    return FakeAppleScriptSender()


@pytest.fixture
def real_sink(
    session_factory: async_sessionmaker,
    allowlist: AllowlistLoader,
) -> MessageSink:
    """Real :class:`MessageSink` against the migrated tmp_path DB.

    No webhook config — we don't exercise webhook fan-out here; the sink
    test suite covers that. We only care about (a) message INSERT, (b)
    cursor advancement, (c) transactional rollback.
    """
    return MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingSink:
    """Captures every ``record_inbound`` call. Used when we need to inspect
    the envelope shape without booting the full SQLAlchemy stack.
    """

    def __init__(self, allowlist: AllowlistLoader) -> None:
        self._allowlist = allowlist
        self.records: list[tuple[Envelope, str, AllowlistStatus]] = []

    async def record_inbound(self, envelope: Envelope, source: str) -> str:
        entry = self._allowlist.resolve(envelope.source, envelope.sender.id)
        status = (
            AllowlistStatus.ALLOWED if entry is not None else AllowlistStatus.UNKNOWN
        )
        self.records.append((envelope, source, status))
        return envelope.id


class _FailingSink:
    """A sink that raises a configured exception on every call.

    Used to verify the cursor does NOT advance when the sink fails: the
    poll loop should re-raise into its outer ``except``, log, and retry
    on the next cycle (cursor unchanged).
    """

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: int = 0

    async def record_inbound(self, envelope: Envelope, source: str) -> str:
        self.calls += 1
        raise self.exc


def _seed_cursor_zero(db_path: Path) -> None:
    """Pre-seed ``connector_state`` cursor to "0" for ``source='imessage'``.

    Used by tests that want the poll loop to drain the entire historical
    fixture backlog. Without this, task #52's fresh-install path seeds
    the cursor to ``MAX(message.ROWID)`` so the historical rows are
    skipped — correct for production but inconvenient for tests that
    rely on the fixture rows reaching the sink.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO connector_state (source, cursor, updated_at) "
            "VALUES (?, ?, ?)",
            ("imessage", "0", "2026-01-01T00:00:00.000000Z"),
        )
        conn.commit()
    finally:
        conn.close()


class _ZeroCursorSessionFactory:
    """Session factory that mimics a ``connector_state`` row with cursor=0.

    Used by tests with ``session_factory=None``-style setups (no real
    DB) when they need the connector to drain the entire fixture
    backlog. After task #52 the default fresh-install behavior is to
    seed past the backlog; this helper restores the pre-#52 "drain
    everything" semantics for the recording-sink tests that don't
    boot the SQLAlchemy engine.

    The factory is only consulted by ``_read_cursor_at_startup``;
    ``_upsert_channel_metadata`` is gated on the same factory and is a
    no-op when ``session_factory`` is ``None``, but here we DO supply
    a factory, so we also need to satisfy that code path. We make the
    second async-with-block transactional UPSERT a no-op via the
    ``_NullSession``.
    """

    class _NullExecResult:
        def first(self) -> tuple | None:
            return ("0",)

    class _NullSession:
        async def __aenter__(self) -> _ZeroCursorSessionFactory._NullSession:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def execute(self, *args: object, **kwargs: object) -> object:
            return _ZeroCursorSessionFactory._NullExecResult()

        def begin(self) -> _ZeroCursorSessionFactory._NullSession:
            return self

    def __call__(self) -> _ZeroCursorSessionFactory._NullSession:
        return self._NullSession()


def _append_inbound_row(
    chat_db: Path,
    *,
    rowid: int,
    text_value: str,
    handle_id: int = 1,
    is_from_me: int = 0,
    chat_id: int = 1,
) -> None:
    """Append a single row to the writable chat.db copy.

    Mirrors the columns ``build_chat_db._seed`` writes for plain
    messages — handle_id=1 is the allowlisted sender; chat_id=1 is the
    DM thread for that handle.
    """
    conn = sqlite3.connect(chat_db)
    try:
        conn.execute("BEGIN")
        date_ns = 1_000_000_000_000_000_000 + rowid * 1_000_000_000
        guid = f"appended-{rowid:020d}"
        conn.execute(
            "INSERT INTO message ("
            " ROWID, guid, text, handle_id, service, account, date, date_read,"
            " date_delivered, is_delivered, is_finished, is_from_me, is_read,"
            " is_sent, cache_has_attachments, item_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rowid,
                guid,
                text_value,
                handle_id,
                "iMessage",
                "E:owner@example.com",
                date_ns,
                0,
                date_ns,
                1,
                1,
                is_from_me,
                0,
                0,
                0,
                0,
            ),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date) "
            "VALUES (?, ?, ?)",
            (chat_id, rowid, date_ns),
        )
        conn.commit()
    finally:
        conn.close()


def _read_cursor(db_path: Path) -> str | None:
    """Read ``connector_state.cursor`` for ``source='imessage'`` directly."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT cursor FROM connector_state WHERE source = ?", ("imessage",)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


def _read_messages_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT count(*) FROM messages WHERE source = ?", ("imessage",)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else 0


def _read_channel_metadata(db_path: Path, channel_id: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM channels "
            "WHERE source = ? AND channel_id = ?",
            ("imessage", channel_id),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


async def _wait_until(
    predicate, timeout: float = 2.0, interval: float = 0.02
) -> bool:
    """Poll ``predicate`` until it returns True or the timeout elapses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Functional: poller picks up new rows within one cycle
# ---------------------------------------------------------------------------


async def test_poller_picks_up_appended_row_within_one_cycle(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """A row appended after start() is persisted within one poll interval.

    With cursor recovery (task #52), a fresh install seeds the cursor to
    ``MAX(message.ROWID)`` so the historical backlog is NOT replayed.
    Only rows appended *after* startup should be drained.
    """
    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        attachment_store=None,  # skip attachment rehosting in this test
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        # Fresh install + max-ROWID seed: no historical rows replayed.
        # Wait one poll cycle to confirm the cursor row stays absent
        # (no message INSERT yet -> sink never advances cursor).
        await asyncio.sleep(TEST_POLL_INTERVAL * 5)
        baseline_count = _read_messages_count(fresh_db)
        assert baseline_count == 0, (
            "fresh install should not replay historical backlog; "
            f"got {baseline_count} messages"
        )

        # Append a brand-new row with a higher ROWID than anything seeded.
        new_rowid = 1000
        _append_inbound_row(
            chat_db_copy, rowid=new_rowid, text_value="appended after start"
        )

        ok_appended = await _wait_until(
            lambda: int(_read_cursor(fresh_db) or "0") >= new_rowid,
            timeout=2.0,
        )
        assert ok_appended, (
            f"poller did not pick up appended ROWID {new_rowid} within timeout; "
            f"cursor={_read_cursor(fresh_db)}"
        )
        assert _read_messages_count(fresh_db) == baseline_count + 1
    finally:
        await connector.stop()


# ---------------------------------------------------------------------------
# Functional: each row produces an envelope through the sink
# ---------------------------------------------------------------------------


async def test_each_row_produces_envelope_through_sink(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
) -> None:
    """Every fixture inbound row reaches the sink as a normalized envelope.

    Uses a stub session_factory that reports cursor=0 so the fixture's
    historical backlog is drained (post-#52 fresh-install would
    otherwise seed past it).
    """
    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_ZeroCursorSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        # Fixture seeds 6 rows: 3 plain, 1 attributedBody, 1 attachment, 1
        # quarantine. All have is_from_me=0, all are DM (style=45).
        ok = await _wait_until(lambda: len(sink.records) >= 6, timeout=2.0)
        assert ok, f"expected 6 envelopes; observed {len(sink.records)}"

        # Every record is a properly-shaped Envelope.
        for envelope, cursor_str, _status in sink.records:
            assert isinstance(envelope, Envelope)
            assert envelope.source is Source.IMESSAGE
            assert envelope.channel_type is ChannelType.DM
            assert envelope.direction is Direction.INBOUND
            # Cursor passed to the sink is the row's ROWID as decimal text.
            assert cursor_str.isdigit()
            assert int(cursor_str) > 0

        # Allowlisted sender records carry status=ALLOWED + display name.
        allowed_records = [
            (e, c, s) for (e, c, s) in sink.records if s is AllowlistStatus.ALLOWED
        ]
        assert len(allowed_records) >= 3
        for envelope, _c, _s in allowed_records:
            assert envelope.sender.id == ALLOWLISTED_HANDLE
            assert envelope.sender.display_name == "Alice"
            assert envelope.sender.person_id == "alice"
            # iMessage channel_id == handle (no prefix per spec §7.3.1).
            assert envelope.channel_id == ALLOWLISTED_HANDLE

        # Quarantined sender records carry status=UNKNOWN.
        unknown_records = [
            (e, c, s) for (e, c, s) in sink.records if s is AllowlistStatus.UNKNOWN
        ]
        assert len(unknown_records) == 1
        assert unknown_records[0][0].sender.id == QUARANTINED_HANDLE
    finally:
        await connector.stop()


# ---------------------------------------------------------------------------
# Edge case: is_from_me=1 rows are skipped
# ---------------------------------------------------------------------------


async def test_is_from_me_rows_skipped(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
) -> None:
    """Outbound (``is_from_me=1``) rows are not handed to the sink.

    Uses a stub session_factory reporting cursor=0 so the connector
    drains the historical fixture rows + the appended outbound row.
    """
    # Append a high-ROWID outbound row before start so we can prove the
    # connector polled past it without recording.
    _append_inbound_row(
        chat_db_copy,
        rowid=999,
        text_value="my own outbound copy",
        is_from_me=1,
    )

    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_ZeroCursorSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        # Wait long enough for at least one poll cycle to drain everything.
        await asyncio.sleep(TEST_POLL_INTERVAL * 5)
        # The is_from_me=1 row must NOT appear among the sink records.
        for envelope, _, _ in sink.records:
            assert envelope.text != "my own outbound copy", (
                "is_from_me=1 row should have been skipped, but reached the sink"
            )
    finally:
        await connector.stop()


# ---------------------------------------------------------------------------
# Edge case: group chat rows skipped (reader's SQL filter)
# ---------------------------------------------------------------------------


async def test_group_chat_rows_skipped(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
) -> None:
    """Group-chat rows (``chat.style != 45``) are filtered out at the reader."""
    # Insert a group chat (style=43) and a message in it. The reader's
    # SQL ``WHERE c.style = 45`` should ensure this row never surfaces.
    conn = sqlite3.connect(chat_db_copy)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO chat ("
            " ROWID, guid, style, state, account_id, chat_identifier,"
            " service_name, account_login, is_archived"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                3,
                "iMessage;+;chat-group",
                43,  # group chat style
                3,
                "ACCT-FIXTURE",
                "group",
                "iMessage",
                "E:owner@example.com",
                0,
            ),
        )
        date_ns = 999_999_999_999_999_999
        conn.execute(
            "INSERT INTO message ("
            " ROWID, guid, text, handle_id, service, account, date, date_read,"
            " date_delivered, is_delivered, is_finished, is_from_me, is_read,"
            " is_sent, cache_has_attachments, item_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                500,
                "group-msg-001",
                "group hello",
                1,
                "iMessage",
                "E:owner@example.com",
                date_ns,
                0,
                date_ns,
                1,
                1,
                0,
                0,
                0,
                0,
                0,
            ),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date) "
            "VALUES (?, ?, ?)",
            (3, 500, date_ns),
        )
        conn.commit()
    finally:
        conn.close()

    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_ZeroCursorSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        await asyncio.sleep(TEST_POLL_INTERVAL * 5)
        for envelope, _, _ in sink.records:
            assert envelope.text != "group hello", (
                "group chat row (style=43) leaked through the DM filter"
            )
    finally:
        await connector.stop()


# ---------------------------------------------------------------------------
# Functional: cursor persisted in connector_state
# ---------------------------------------------------------------------------


async def test_cursor_persisted_in_connector_state_after_poll(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """The sink's cursor advance is observable in ``connector_state``.

    Pre-seeds cursor=0 so the historical fixture rows are drained
    (post-#52 fresh-install would otherwise seed past them).
    """
    _seed_cursor_zero(fresh_db)
    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        ok = await _wait_until(
            lambda: _read_cursor(fresh_db) is not None
            and int(_read_cursor(fresh_db)) >= 1,
            timeout=2.0,
        )
        assert ok, (
            f"expected connector_state cursor to advance; got {_read_cursor(fresh_db)}"
        )
        cursor = int(_read_cursor(fresh_db))
        assert cursor >= 5  # fixture seeds 6 inbound rows; cursor reaches the last
    finally:
        await connector.stop()


# ---------------------------------------------------------------------------
# Functional: connector restart resumes from cursor
# ---------------------------------------------------------------------------


async def test_connector_restart_resumes_from_cursor(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """A restart starts polling from the persisted cursor, not ROWID 0.

    First run pre-seeds cursor=0 so the historical backlog is drained,
    establishing the cursor we then verify the restart honors.
    """
    _seed_cursor_zero(fresh_db)
    reader1 = ChatDbReader(chat_db_copy)
    connector1 = ImessageConnector(
        reader=reader1,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    await connector1.start()
    try:
        ok = await _wait_until(
            lambda: _read_messages_count(fresh_db) >= 6, timeout=2.0
        )
        assert ok, "first run did not drain initial rows"
        first_cursor = int(_read_cursor(fresh_db))
        first_count = _read_messages_count(fresh_db)
    finally:
        await connector1.stop()

    # New reader + connector pointed at the same chat.db / amg.db.
    reader2 = ChatDbReader(chat_db_copy)
    connector2 = ImessageConnector(
        reader=reader2,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    await connector2.start()
    try:
        # No new rows since shutdown -> message count unchanged after at
        # least one poll cycle. If the cursor weren't honored, every
        # initial row would be re-INSERTed (and IntegrityError-rolled-back,
        # but the sink would log).
        await asyncio.sleep(TEST_POLL_INTERVAL * 5)
        assert _read_messages_count(fresh_db) == first_count
        assert int(_read_cursor(fresh_db)) == first_cursor
    finally:
        await connector2.stop()


# ---------------------------------------------------------------------------
# Functional: sink failure does NOT advance cursor
# ---------------------------------------------------------------------------


async def test_sink_failure_does_not_advance_cursor(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """When the sink raises mid-cycle, the cursor stays put for that row.

    Pre-seeds cursor=0 so the historical fixture rows are surfaced to
    the failing sink (post-#52 fresh-install would otherwise seed past
    them and the sink would never be invoked).
    """
    _seed_cursor_zero(fresh_db)
    failing_sink = _FailingSink(RuntimeError("disk full"))
    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=failing_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        # Wait until the sink has been called at least once (so we know
        # the poller hit a row that raised).
        ok = await _wait_until(lambda: failing_sink.calls >= 1, timeout=2.0)
        assert ok, "sink was never called"

        # Cursor must remain at the pre-seeded "0" — the sink rolled
        # back so no advancement happened.
        cursor_value = _read_cursor(fresh_db)
        assert cursor_value == "0", (
            f"cursor advanced despite sink failure; got {cursor_value!r}"
        )
        # And no messages were persisted.
        assert _read_messages_count(fresh_db) == 0
    finally:
        await connector.stop()


# ---------------------------------------------------------------------------
# Functional: send returns SendResult.ok=False -> upstream maps to PLATFORM_SEND_FAILED
# ---------------------------------------------------------------------------


def _ensure_logger_capturable(name: str) -> None:
    """Re-enable a logger that an earlier alembic upgrade muted.

    Alembic's ``env.py`` calls :func:`logging.config.fileConfig` which
    defaults to ``disable_existing_loggers=True`` — that flips
    ``logger.disabled = True`` on every logger created before the
    config call, which kills pytest's ``caplog`` handler attachment.
    Tests that expect caplog to see records from one of our loggers
    call this helper to flip ``disabled`` back to ``False``.
    """
    logger = logging.getLogger(name)
    logger.disabled = False
    logger.propagate = True


@pytest.fixture(autouse=True)
def _restore_amg_loggers_after_alembic() -> Iterator[None]:
    """Restore ``amg.*`` loggers after tests in this file run.

    This module's ``fresh_db`` fixture runs ``alembic upgrade head``,
    which calls ``logging.config.fileConfig`` with
    ``disable_existing_loggers=True``. That flips
    ``logger.disabled = True`` on every logger created before the call,
    breaking pytest's ``caplog`` handler in *subsequent* tests
    (notably ``test_reader.py::test_database_locked_logs_and_raises_for_caller_retry``).

    The autouse fixture re-enables every ``amg.*`` logger after each
    test so cross-file ordering stays clean. Cheap: walks the small
    fixed set of loggers AMG creates.
    """
    yield
    for name in (
        "amg.connectors.imessage.connector",
        "amg.connectors.imessage.reader",
        "amg.connectors.imessage.applescript",
        "amg.connectors.discord.connector",
        "amg.core.allowlist",
        "amg.core.attachments",
        "amg.core.auth",
        "amg.core.errors",
        "amg.core.idempotency",
        "amg.core.logging",
        "amg.core.message_sink",
        "amg.core.rate_limit",
        "amg.core.sweepers",
        "amg.core.webhook",
    ):
        logger = logging.getLogger(name)
        logger.disabled = False


async def test_send_failure_returns_platform_send_failed(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AppleScript failure surfaces as ``SendResult(ok=False)``; we log ERROR.

    The HTTP route translates ``ok=False`` into ``502 PLATFORM_SEND_FAILED``
    (spec §7.4 / §13 R-2); that route translation is covered by the
    ``POST /messages/send`` test suite. Here we verify the connector
    propagates ``ok=False`` and emits an ERROR-level log so the route
    has something to translate.

    We deliberately skip the SQLite session_factory (and therefore the
    Alembic upgrade) because alembic's ``fileConfig`` disables existing
    loggers, breaking pytest's ``caplog`` handler attachment. The send
    path doesn't need persistence — the connector falls back to
    ``iMessage;-;<channel_id>`` when no metadata is configured.
    """
    _ensure_logger_capturable("amg.connectors.imessage.connector")
    fake_sender = FakeAppleScriptSender()
    fake_sender.queue_outcome(
        SendResult(ok=False, error="timeout", raw_stderr="osascript hung")
    )

    sink = _RecordingSink(allowlist)
    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=None,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    # Don't start the poller — we only exercise the outbound path.
    with caplog.at_level(logging.ERROR, logger="amg.connectors.imessage.connector"):
        result = await connector.send(ALLOWLISTED_HANDLE, "hello")

    assert result.ok is False
    assert result.error == "timeout"
    # We logged the failure at ERROR so the upstream route has visibility.
    assert any(
        "imessage_send_failed" in record.getMessage() for record in caplog.records
    )

    await connector.stop()


# ---------------------------------------------------------------------------
# Functional: send routes to chat.guid from channels.metadata_json
# ---------------------------------------------------------------------------


async def test_send_uses_metadata_json_chat_guid(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """``send`` reads ``chat.guid`` from ``channels.metadata_json``.

    Pre-seeds cursor=0 so the historical fixture rows drain and
    populate ``channels.metadata_json`` with the ``chat.guid``.
    """
    _seed_cursor_zero(fresh_db)
    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        # Drain initial rows so channels.metadata_json is populated.
        ok = await _wait_until(
            lambda: _read_channel_metadata(fresh_db, ALLOWLISTED_HANDLE) is not None,
            timeout=2.0,
        )
        assert ok, "metadata_json was never populated for the allowlisted channel"
        metadata = _read_channel_metadata(fresh_db, ALLOWLISTED_HANDLE)
        assert metadata is not None
        assert CHAT_GUID in metadata  # JSON-encoded chat.guid pinned

        # Now send — the connector should resolve the metadata's chat.guid.
        result = await connector.send(ALLOWLISTED_HANDLE, "outbound")
        assert result.ok is True
        assert len(fake_sender.calls) == 1
        recorded = fake_sender.calls[-1]
        assert recorded.chat_guid == CHAT_GUID
        assert recorded.text == "outbound"
        assert recorded.attachment_path is None
    finally:
        await connector.stop()


# ---------------------------------------------------------------------------
# Functional: send fallback when no metadata exists yet
# ---------------------------------------------------------------------------


async def test_send_falls_back_to_canonical_chat_guid(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
) -> None:
    """First-send-to-an-unseen-channel uses ``iMessage;-;<channel_id>``."""
    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    # Skip start() — no inbound has populated channels for "+15550000001".
    result = await connector.send("+15550000001", "first contact")

    assert result.ok is True
    assert len(fake_sender.calls) == 1
    assert fake_sender.calls[-1].chat_guid == "iMessage;-;+15550000001"

    await connector.stop()


# ---------------------------------------------------------------------------
# Error handling: chat.db missing -> degraded; logs ERROR
# ---------------------------------------------------------------------------


async def test_missing_chat_db_raises_at_reader_construction(
    tmp_path: Path,
) -> None:
    """Missing chat.db -> reader raises ``ChatDbPathMissingError`` at construct.

    Per spec §5.1 error handling: a missing chat.db (FDA not granted)
    is detected at reader-construction time. The adapter's lifespan
    sees the exception before instantiating the connector and surfaces
    ``connectors.imessage.state="degraded"`` in ``/healthz``.

    The connector itself never gets built in that path; this test
    just pins the contract that ``ChatDbReader`` raises early.
    """
    missing_path = tmp_path / "nonexistent" / "chat.db"
    with pytest.raises(ChatDbPathMissingError):
        ChatDbReader(missing_path)


async def test_cursor_read_failure_logs_error_and_treats_as_fresh_install(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cursor read failure -> log ERROR + fresh-install fallback (task #52).

    Per task #52 acceptance: "Cursor read fails -> log ERROR, treat as
    fresh install". The connector must NOT mark itself degraded on a
    transient AMG DB hiccup; instead it logs ERROR and seeds the
    in-memory cursor from ``MAX(message.ROWID)`` in chat.db so the
    poll loop starts cleanly. The next successful sink transaction
    heals the ``connector_state`` row.
    """
    _ensure_logger_capturable("amg.connectors.imessage.connector")

    class _BrokenSessionFactory:
        def __call__(self) -> object:
            raise sqlite3.OperationalError("unable to open database file")

    class _NoopSink:
        calls: list[str] = []

        async def record_inbound(self, envelope: Envelope, source: str) -> str:
            self.calls.append(source)
            return envelope.id

    reader = ChatDbReader(chat_db_copy)
    sink = _NoopSink()
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_BrokenSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    with caplog.at_level(logging.ERROR, logger="amg.connectors.imessage.connector"):
        await connector.start()

    # The poll loop is running (NOT degraded).
    assert connector.degraded is False

    # ERROR was logged before falling back to fresh-install seeding.
    assert any(
        "imessage_cursor_read_failed_treating_as_fresh_install" in record.getMessage()
        for record in caplog.records
    ), (
        f"expected ERROR log on cursor read failure; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    # Wait one poll cycle; with cursor seeded to MAX(ROWID)=6 and no
    # new rows, the sink must NOT receive any of the fixture rows.
    await asyncio.sleep(TEST_POLL_INTERVAL * 5)
    assert sink.calls == [], (
        "fresh-install seed should skip the historical backlog; "
        f"sink received {sink.calls}"
    )

    await connector.stop()


async def test_default_poll_interval_is_one_second() -> None:
    """The module's default poll interval matches the spec acceptance criterion."""
    # Spec §5.1 acceptance: "Poller picks up new rows within 1s".
    assert DEFAULT_POLL_INTERVAL_SECONDS == 1.0


# ---------------------------------------------------------------------------
# Sanity: timestamp on envelope is timezone-aware UTC
# ---------------------------------------------------------------------------


async def test_envelope_timestamp_is_tz_aware_utc(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
) -> None:
    """Every emitted envelope has a tz-aware UTC ``timestamp``."""
    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_ZeroCursorSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        ok = await _wait_until(lambda: len(sink.records) >= 1, timeout=2.0)
        assert ok
        for envelope, _, _ in sink.records:
            assert isinstance(envelope.timestamp, datetime)
            assert envelope.timestamp.tzinfo is not None
            assert envelope.timestamp.utcoffset() == UTC.utcoffset(None)
    finally:
        await connector.stop()


# ---------------------------------------------------------------------------
# Cursor recovery (task #52) — explicit acceptance criteria
# ---------------------------------------------------------------------------


async def test_cursor_recovery_fresh_install_seeds_to_max_rowid_no_flood(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """Fresh install (no cursor row): seed to ``MAX(ROWID)``; no backlog flood.

    Acceptance criterion #1: "Fresh install: cursor seeded to max ROWID;
    no historical flood".

    The fixture chat.db has 6 inbound rows. With no ``connector_state``
    row at startup, the connector must seed past ROWID 6 and refrain
    from emitting any of the historical rows. Only rows appended
    *after* startup with ROWID > 6 should reach the sink.
    """
    # Sanity: confirm the fixture's max ROWID is non-zero so the
    # "no flood" assertion is meaningful.
    conn = sqlite3.connect(chat_db_copy)
    try:
        max_rowid = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0]
    finally:
        conn.close()
    assert max_rowid >= 6, "fixture changed; expected at least 6 seeded rows"

    # Confirm no connector_state row exists (true fresh install).
    assert _read_cursor(fresh_db) is None

    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        # Wait several poll cycles; no historical row should be replayed.
        await asyncio.sleep(TEST_POLL_INTERVAL * 5)
        assert _read_messages_count(fresh_db) == 0, (
            "fresh install should NOT replay historical chat.db rows"
        )
        # connector_state stays empty until the next inbound row's
        # sink transaction; the in-memory seed is not eagerly persisted.
        assert _read_cursor(fresh_db) is None
    finally:
        await connector.stop()


async def test_cursor_recovery_restart_resumes_no_duplicates(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """Restart with a persisted cursor resumes forward without duplicates.

    Acceptance criterion #2: "Restart with cursor: resumes from where
    it left off, no duplicates".

    Sequence:
    1. Pre-seed cursor=0 + start connector1; drain initial 6 rows.
    2. Stop connector1; record cursor + message count.
    3. Append a brand-new row with ROWID > stored cursor.
    4. Start connector2 against the same DBs; it should resume from
       the persisted cursor and pick up only the appended row — no
       re-emission of the 6 historical rows.
    """
    _seed_cursor_zero(fresh_db)

    reader1 = ChatDbReader(chat_db_copy)
    connector1 = ImessageConnector(
        reader=reader1,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    await connector1.start()
    try:
        ok = await _wait_until(
            lambda: _read_messages_count(fresh_db) >= 6, timeout=2.0
        )
        assert ok, "first run did not drain the historical rows"
        first_count = _read_messages_count(fresh_db)
        first_cursor = int(_read_cursor(fresh_db))
        assert first_cursor >= 6
    finally:
        await connector1.stop()

    # Append a new row beyond the stored cursor while the connector is down.
    new_rowid = first_cursor + 100
    _append_inbound_row(
        chat_db_copy,
        rowid=new_rowid,
        text_value="appended between runs",
    )

    reader2 = ChatDbReader(chat_db_copy)
    connector2 = ImessageConnector(
        reader=reader2,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    await connector2.start()
    try:
        ok = await _wait_until(
            lambda: int(_read_cursor(fresh_db) or "0") >= new_rowid,
            timeout=2.0,
        )
        assert ok, (
            f"restart did not pick up appended ROWID {new_rowid}; "
            f"cursor={_read_cursor(fresh_db)}"
        )
        # Exactly ONE new row was inserted — no duplicates of the
        # historical 6.
        assert _read_messages_count(fresh_db) == first_count + 1, (
            "restart should resume forward only; "
            f"expected {first_count + 1} messages, got {_read_messages_count(fresh_db)}"
        )
    finally:
        await connector2.stop()


async def test_cursor_recovery_only_advances_on_insert_success(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """Cursor only advances if the message INSERT transaction commits.

    Acceptance criterion #3: "Cursor only advances if INSERT succeeds".

    The advancement happens inside the sink's transaction; if the sink
    raises, the surrounding ``BEGIN ... COMMIT`` rolls back and the
    cursor row stays absent (or unchanged from its prior value).

    Variant of ``test_sink_failure_does_not_advance_cursor`` that
    pre-seeds cursor=1 to assert the cursor stays at 1 (does NOT
    advance to a higher ROWID) even when fetch_new returns rows the
    sink then rejects.
    """
    # Pre-seed cursor=1 so the sink will be invoked for ROWIDs 2..6.
    conn = sqlite3.connect(fresh_db)
    try:
        conn.execute(
            "INSERT INTO connector_state (source, cursor, updated_at) "
            "VALUES (?, ?, ?)",
            ("imessage", "1", "2026-01-01T00:00:00.000000Z"),
        )
        conn.commit()
    finally:
        conn.close()
    assert _read_cursor(fresh_db) == "1"

    failing_sink = _FailingSink(RuntimeError("disk full"))
    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=failing_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        ok = await _wait_until(lambda: failing_sink.calls >= 1, timeout=2.0)
        assert ok, "sink was never called"
        # Wait additional cycles to let the connector retry and confirm
        # no advancement happens.
        await asyncio.sleep(TEST_POLL_INTERVAL * 3)

        # Cursor stays at "1" — never advanced past the failing row.
        assert _read_cursor(fresh_db) == "1", (
            f"cursor advanced despite sink failures; got {_read_cursor(fresh_db)!r}"
        )
        # And no messages were persisted.
        assert _read_messages_count(fresh_db) == 0
    finally:
        await connector.stop()


async def test_cursor_recovery_stale_cursor_polls_forward(
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    fresh_db: Path,
) -> None:
    """Stale cursor (chat.db advanced past it): poll forward correctly.

    Acceptance criterion #4: "Cursor row exists but stale: still polls
    forward correctly".

    Pre-seeds the cursor at a value below the fixture's max ROWID
    (cursor=2 < max=6). On start, the connector must resume from
    ROWID 3 and emit ROWIDs 3..6, advancing the cursor to 6.
    """
    # Pre-seed a stale cursor (rowid=2, well below fixture's max=6).
    conn = sqlite3.connect(fresh_db)
    try:
        conn.execute(
            "INSERT INTO connector_state (source, cursor, updated_at) "
            "VALUES (?, ?, ?)",
            ("imessage", "2", "2026-01-01T00:00:00.000000Z"),
        )
        conn.commit()
    finally:
        conn.close()

    reader = ChatDbReader(chat_db_copy)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        ok = await _wait_until(
            lambda: int(_read_cursor(fresh_db) or "0") >= 6, timeout=2.0
        )
        assert ok, (
            f"stale cursor did not advance; got {_read_cursor(fresh_db)!r}"
        )
        # Fixture has 6 inbound rows; ROWIDs 3..6 = 4 rows surfaced.
        # All 4 are inbound (no is_from_me=1 in the seeded set).
        count = _read_messages_count(fresh_db)
        assert count == 4, (
            f"expected 4 rows surfaced from stale cursor (rowids 3..6); got {count}"
        )
    finally:
        await connector.stop()


async def test_cursor_recovery_no_session_factory_seeds_from_chat_db(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
) -> None:
    """``session_factory=None`` (test fakes): seed from chat.db max ROWID.

    With no AMG DB configured, ``_read_cursor_at_startup`` returns
    ``None`` (treated as fresh install). The seed reads
    ``MAX(message.ROWID)`` from chat.db so the historical backlog is
    NOT replayed even in test setups without an AMG DB.
    """
    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=None,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        await asyncio.sleep(TEST_POLL_INTERVAL * 5)
        assert sink.records == [], (
            "fresh install with no session_factory should still skip backlog"
        )

        # Append a new row beyond fixture max (6) and confirm it surfaces.
        _append_inbound_row(chat_db_copy, rowid=999, text_value="post-startup")
        ok = await _wait_until(lambda: len(sink.records) >= 1, timeout=2.0)
        assert ok, "appended row was not surfaced after fresh-install seed"
        assert sink.records[-1][0].text == "post-startup"
    finally:
        await connector.stop()


async def test_seed_from_chat_db_returns_zero_when_reader_has_no_path(
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
) -> None:
    """Reader fakes without a ``.path`` attribute -> seed defaults to 0.

    Defensive: the fresh-install seeder uses ``getattr(reader, "path",
    None)``. Test doubles that don't expose ``.path`` (e.g. the
    ``_NoopReader`` used in some smoke tests) trigger the conservative
    fallback of seeding the cursor to 0.
    """
    class _NoPathReader:
        async def fetch_new(self, last_seen_rowid: int) -> list[dict]:
            return []

        async def close(self) -> None:
            return None

    reader = _NoPathReader()
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,  # type: ignore[arg-type]
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=None,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    # Direct-call the helper to verify the fallback explicitly.
    assert connector._seed_cursor_from_chat_db() == 0

    # And that start() succeeds with that fallback (poll loop spins on
    # an empty reader).
    await connector.start()
    await asyncio.sleep(TEST_POLL_INTERVAL * 2)
    assert sink.records == []
    await connector.stop()


async def test_seed_from_chat_db_returns_zero_when_chat_db_unreadable(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """chat.db read failure during seeding -> log WARN + fall back to 0.

    Defensive: if ``MAX(ROWID)`` query fails (corrupt file, permission
    error, etc.) the connector logs a WARN and uses cursor=0. The
    poll loop will then attempt to drain from ROWID 0 — the
    conservative fallback that prefers replay over silent skip when
    the chat.db itself can't be inspected.
    """
    _ensure_logger_capturable("amg.connectors.imessage.connector")

    # Build a connector pointed at a real chat.db, then replace the
    # reader's path with a non-existent file so the seed query fails.
    reader = ChatDbReader(chat_db_copy)
    # Swap the path attribute to something invalid for the seed-query
    # only. The reader's existing connection still works because it's
    # already opened (or opens lazily against the original path).
    reader._path = chat_db_copy.parent / "does_not_exist.db"  # type: ignore[attr-defined]

    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=None,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    with caplog.at_level(logging.WARNING, logger="amg.connectors.imessage.connector"):
        seed = connector._seed_cursor_from_chat_db()

    assert seed == 0
    assert any(
        "imessage_seed_cursor_failed_using_zero" in record.getMessage()
        for record in caplog.records
    ), (
        f"expected WARN log on seed failure; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    await connector.stop()


# ---------------------------------------------------------------------------
# Mac-wake / ROWID-gap detection (task #53)
# ---------------------------------------------------------------------------


def _append_inbound_rows_bulk(
    chat_db: Path,
    *,
    rowids: list[int],
    text_prefix: str = "gap-row",
    handle_id: int = 1,
    chat_id: int = 1,
) -> None:
    """Insert N rows in one transaction at the supplied (sparse) ROWIDs.

    Used by the gap tests to simulate a Mac-wake backlog where the OS
    Messages.app catches up and writes a contiguous (or sparse) batch
    of new messages all at once. Sparse ROWIDs are allowed to test that
    the connector measures the gap against ``max(ROWID)`` (not row
    count) — a sleeping Mac can leave behind ROWIDs Apple's daemon
    consumed for system-internal items the chat-message join filters out.
    """
    conn = sqlite3.connect(chat_db)
    try:
        conn.execute("BEGIN")
        for rowid in rowids:
            date_ns = 1_000_000_000_000_000_000 + rowid * 1_000_000_000
            guid = f"gap-{rowid:020d}"
            conn.execute(
                "INSERT INTO message ("
                " ROWID, guid, text, handle_id, service, account, date, date_read,"
                " date_delivered, is_delivered, is_finished, is_from_me, is_read,"
                " is_sent, cache_has_attachments, item_type"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rowid,
                    guid,
                    f"{text_prefix}-{rowid}",
                    handle_id,
                    "iMessage",
                    "E:owner@example.com",
                    date_ns,
                    0,
                    date_ns,
                    1,
                    1,
                    0,
                    0,
                    0,
                    0,
                    0,
                ),
            )
            conn.execute(
                "INSERT INTO chat_message_join (chat_id, message_id, message_date) "
                "VALUES (?, ?, ?)",
                (chat_id, rowid, date_ns),
            )
        conn.commit()
    finally:
        conn.close()


def test_rowid_gap_warn_threshold_default() -> None:
    """The module's gap WARN threshold matches the task #53 contract."""
    assert ROWID_GAP_WARN_THRESHOLD == 100


async def test_rowid_gap_above_threshold_logs_warn(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gap > 100 between cursor and max(ROWID) emits a single WARN.

    Acceptance criterion #1: "Gap > 100 logs WARN with cursor + max ROWID".

    Pre-seed cursor=0 (drain the historical fixture rows + the appended
    backlog). Inject a non-contiguous batch with ROWIDs jumping past the
    100-row threshold (sleeping-Mac scenario where the OS resumed and
    appended a backlog). The connector must log WARN with both the
    last cursor and the new max ROWID.
    """
    _ensure_logger_capturable("amg.connectors.imessage.connector")

    # Inject a sparse backlog: a few rows clustered well past the
    # fixture's max ROWID (=6) so the gap from cursor=0 jumps to ~520.
    _append_inbound_rows_bulk(
        chat_db_copy, rowids=[500, 510, 520], text_prefix="wake"
    )

    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_ZeroCursorSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    with caplog.at_level(logging.WARNING, logger="amg.connectors.imessage.connector"):
        await connector.start()
        try:
            # Wait for the first poll cycle to drain everything.
            ok = await _wait_until(lambda: len(sink.records) >= 9, timeout=2.0)
            assert ok, (
                f"expected 9 records (6 fixture + 3 backlog); got {len(sink.records)}"
            )
        finally:
            await connector.stop()

    gap_records = [
        r
        for r in caplog.records
        if "imessage_connector_rowid_gap" in r.getMessage()
    ]
    assert len(gap_records) >= 1, (
        f"expected WARN on rowid gap; got: {[r.getMessage() for r in caplog.records]}"
    )
    record = gap_records[0]
    # Both the structured `extra` keys must be attached to the LogRecord.
    assert getattr(record, "last_cursor", None) == 0
    assert getattr(record, "new_max", None) == 520
    assert getattr(record, "reason", None) == "rowid_gap"
    assert getattr(record, "component", None) == "imessage_connector"


async def test_rowid_gap_at_or_below_threshold_no_warn(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gap <= 100 (continuous traffic): no WARN logged.

    Acceptance criterion #5: "Continuous traffic: no WARN spam".

    Pre-seed cursor=0. The fixture seeds 6 rows (gap=6 from cursor=0),
    well under the 100-row threshold; appending a single fresh row
    keeps the per-cycle gap small (<=100). The connector must NOT
    emit the gap WARN.
    """
    _ensure_logger_capturable("amg.connectors.imessage.connector")

    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_ZeroCursorSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    with caplog.at_level(logging.WARNING, logger="amg.connectors.imessage.connector"):
        await connector.start()
        try:
            # Drain the historical 6 rows.
            ok = await _wait_until(lambda: len(sink.records) >= 6, timeout=2.0)
            assert ok, "fixture rows did not drain"

            # Simulate continuous traffic: append a few rows one at a time,
            # each with a small gap from the prior cursor.
            for i, rowid in enumerate([10, 12, 15, 20, 50, 100], start=1):
                _append_inbound_rows_bulk(
                    chat_db_copy, rowids=[rowid], text_prefix=f"steady-{i}"
                )
                await asyncio.sleep(TEST_POLL_INTERVAL * 2)
        finally:
            await connector.stop()

    gap_records = [
        r
        for r in caplog.records
        if "imessage_connector_rowid_gap" in r.getMessage()
    ]
    assert gap_records == [], (
        f"continuous traffic should not log rowid_gap WARN; "
        f"got: {[r.getMessage() for r in gap_records]}"
    )


async def test_rowid_gap_large_backlog_processes_all_rows_in_order(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
) -> None:
    """200-row backlog: every row processed in ASC ROWID order, no duplicates.

    Acceptance criteria #2-#4: "All gap rows processed in order", "No
    duplicates", "Very large gap (200 rows): processed".

    Inject 200 contiguous rows past the fixture's max. Confirm:
    * Every appended ROWID reaches the sink.
    * Sink records arrive in ascending ROWID order.
    * No ROWID is delivered twice.
    """
    _ensure_logger_capturable("amg.connectors.imessage.connector")

    backlog = list(range(100, 300))  # 200 rows, ROWIDs 100..299
    _append_inbound_rows_bulk(chat_db_copy, rowids=backlog, text_prefix="backlog")

    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_ZeroCursorSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    await connector.start()
    try:
        # 6 fixture rows + 200 backlog = 206 expected envelopes.
        ok = await _wait_until(lambda: len(sink.records) >= 206, timeout=5.0)
        assert ok, (
            f"expected 206 records (6 fixture + 200 backlog); "
            f"got {len(sink.records)}"
        )
    finally:
        await connector.stop()

    # Extract the rowids the sink saw, in arrival order.
    arrival_rowids = [int(cursor_str) for _env, cursor_str, _status in sink.records]

    # All backlog ROWIDs are present.
    arrival_set = set(arrival_rowids)
    missing = set(backlog) - arrival_set
    assert not missing, f"backlog rowids missing from sink delivery: {sorted(missing)}"

    # No duplicates.
    assert len(arrival_rowids) == len(arrival_set), (
        "sink received duplicate rowids during gap processing"
    )

    # Strictly ascending ROWID order across the entire delivery.
    assert arrival_rowids == sorted(arrival_rowids), (
        "rows were not delivered in ascending ROWID order"
    )


async def test_rowid_gap_sparse_backlog_uses_max_rowid_for_gap(
    chat_db_copy: Path,
    allowlist: AllowlistLoader,
    fake_sender: FakeAppleScriptSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gap is measured against ``max(ROWID)``, not row count.

    Edge case: a sparse backlog of just 3 rows with ROWIDs spread to
    501 still trips the WARN because the max ROWID jump from cursor=0
    is 501 (>100). The WARN's ``new_max`` must reflect that max ROWID.
    """
    _ensure_logger_capturable("amg.connectors.imessage.connector")

    _append_inbound_rows_bulk(
        chat_db_copy, rowids=[200, 350, 501], text_prefix="sparse"
    )

    reader = ChatDbReader(chat_db_copy)
    sink = _RecordingSink(allowlist)
    connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=_ZeroCursorSessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )

    with caplog.at_level(logging.WARNING, logger="amg.connectors.imessage.connector"):
        await connector.start()
        try:
            ok = await _wait_until(lambda: len(sink.records) >= 9, timeout=2.0)
            assert ok, f"expected 9 records (6 fixture + 3 sparse); got {len(sink.records)}"
        finally:
            await connector.stop()

    gap_records = [
        r
        for r in caplog.records
        if "imessage_connector_rowid_gap" in r.getMessage()
    ]
    assert gap_records, "expected gap WARN for sparse backlog"
    assert getattr(gap_records[0], "new_max", None) == 501
    assert getattr(gap_records[0], "last_cursor", None) == 0
