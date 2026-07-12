"""Cursor-recovery / no-duplicates end-to-end test (task #56; spec §5.1, §6.4 RPO).

Verifies the iMessage connector's persisted-cursor recovery contract:

* **No duplicates**: rows that the connector drained before a stop are
  NOT re-emitted to the sink after restart. The persisted
  ``connector_state.cursor`` for ``source='imessage'`` is the source of
  truth — on restart, the next ``ChatDbReader.fetch_new`` clause uses
  ``WHERE ROWID > cursor`` so already-seen rows are filtered at SQL.
* **No lost rows**: rows appended *while the connector is down* are
  picked up on the very next poll cycle after restart, in ROWID-ASC
  order, and the cursor advances to ``MAX(message.ROWID)``.

Scenario walked end-to-end
--------------------------

1. Build a fresh AMG SQLite DB (``alembic upgrade head``) and a writable
   copy of the Phase 0 chat.db fixture.
2. Construct + start the real
   :class:`amg.connectors.imessage.connector.ImessageConnector` against
   the real :class:`amg.core.message_sink.MessageSink` (no webhook
   config — webhook fan-out is out of scope here).
3. Inject 5 inbound chat.db rows with ROWIDs strictly greater than the
   fixture's ``MAX(message.ROWID)`` (so the fresh-install seed past the
   backlog is irrelevant).
4. Wait ~2 poll cycles + verify all 5 reach ``messages`` and the
   persisted ``connector_state.cursor`` advances to the highest of the
   five.
5. Stop the connector (await its background task cancellation -- this
   is what ``connector.stop()`` does internally per spec §5.1, mirroring
   how a graceful shutdown drains the in-flight cycle).
6. Inject 5 more chat.db rows *while the connector is stopped* with
   ROWIDs strictly greater than the previous batch.
7. Construct + start a second connector against the SAME AMG DB and
   chat.db copy. The cursor row from step 4 is the recovery trigger.
8. Assert:
   * All 10 injected rows are present in ``messages`` exactly once
     (the first 5 are NOT re-emitted -- the WHERE ROWID > cursor clause
     filters them at SQL).
   * ``connector_state.cursor`` advanced to the maximum injected ROWID.

Why this lives in :mod:`tests.e2e` (not :mod:`tests.connectors.imessage`)
------------------------------------------------------------------------

The connector unit tests under
``tests/connectors/imessage/test_connector.py`` cover the cursor-recovery
state machine in isolation (fresh-install seed, stale-cursor forward
poll, sink-failure no-advance, etc). This e2e test is the *behavior*
proof that exercises the full stop/start lifecycle the spec promises:
the same code paths a launchd restart or operator-initiated stop would
hit. It uses the shared :mod:`tests.e2e._helpers` API to keep the wiring
identical to the other e2e tests (crash-relaunch, webhook retry).
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from amg.connectors.imessage.connector import ImessageConnector
from amg.connectors.imessage.reader import ChatDbReader
from amg.core.allowlist import AllowlistLoader
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.envelope import Source
from amg.core.message_sink import MessageSink
from tests.e2e._helpers import (
    append_chat_db_inbound_row,
    apply_alembic_head,
    build_inmemory_allowlist,
    list_messages_for_assertion,
    read_imessage_cursor,
    read_messages_count,
)
from tests.fakes.applescript import FakeAppleScriptSender
from tests.fixtures.build_chat_db import (
    ALLOWLISTED_HANDLE,
    ensure_chat_db,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Aggressive poll interval so two cycles complete in well under a second.
TEST_POLL_INTERVAL = 0.05

# Bound for "rows materialize after restart". The connector needs roughly
# one poll interval; 5s is the same RTO budget used by the other e2e tests.
DRAIN_TIMEOUT_SECONDS = 5.0

# First injected ROWID. The fixture seeds 6 rows (ROWIDs 1..6) so any
# value >= 100 is comfortably past the fresh-install seed watermark.
FIRST_INJECTED_ROWID = 100
PRE_STOP_BATCH_SIZE = 5
POST_STOP_BATCH_SIZE = 5
TOTAL_INJECTED = PRE_STOP_BATCH_SIZE + POST_STOP_BATCH_SIZE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def src_chat_db_path() -> Path:
    """Materialize the Phase 0 fixture chat.db once per module."""
    return ensure_chat_db()


@pytest.fixture
def chat_db_copy(src_chat_db_path: Path, tmp_path: Path) -> Path:
    """Per-test writable copy of the chat.db fixture."""
    dst = tmp_path / "chat.db"
    shutil.copy2(src_chat_db_path, dst)
    return dst


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Fresh AMG SQLite file with the full migrations applied."""
    db_path = tmp_path / "amg-cursor-recovery.db"
    monkeypatch.setenv("AMG_DB_PATH", str(db_path))
    apply_alembic_head(ALEMBIC_INI, db_path)
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
    """Loader that admits the fixture's allowlisted iMessage handle."""
    return build_inmemory_allowlist(
        [
            (
                Source.IMESSAGE,
                ALLOWLISTED_HANDLE,
                "Allowlisted Alice",
                "person_alice",
            ),
        ]
    )


@pytest.fixture
def real_sink(
    session_factory: async_sessionmaker,
    allowlist: AllowlistLoader,
) -> MessageSink:
    """Real sink against the migrated AMG DB. No webhook fan-out."""
    return MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_cursor_at_least(
    db_path: Path,
    target_rowid: int,
    *,
    timeout: float,
    label: str,
) -> None:
    """Poll until ``connector_state.cursor`` (imessage) >= ``target_rowid``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        cursor = read_imessage_cursor(db_path)
        if cursor is not None and int(cursor) >= target_rowid:
            return
        await asyncio.sleep(0.02)
    actual = read_imessage_cursor(db_path)
    raise TimeoutError(
        f"{label}: expected imessage cursor >= {target_rowid} within {timeout}s; "
        f"observed {actual!r}"
    )


def _max_chat_db_rowid(chat_db: Path) -> int:
    """Return ``MAX(message.ROWID)`` from the chat.db copy (or 0 if empty)."""
    conn = sqlite3.connect(chat_db)
    try:
        row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_cursor_recovery_no_duplicates_across_stop_restart(
    fresh_db: Path,
    chat_db_copy: Path,
    session_factory: async_sessionmaker,
    real_sink: MessageSink,
    allowlist: AllowlistLoader,
) -> None:
    """Stop mid-stream, append while down, restart -> all 10 rows, no duplicates.

    See the module docstring for the full scenario walkthrough.
    """
    fake_sender = FakeAppleScriptSender()

    # ------------------------------------------------------------------
    # Phase 1 -- boot the first connector instance and inject 5 rows.
    # ------------------------------------------------------------------
    reader1 = ChatDbReader(chat_db_copy)
    connector1 = ImessageConnector(
        reader=reader1,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        attachment_store=None,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    await connector1.start()

    pre_stop_rowids: list[int] = []
    try:
        # Sanity: fresh install -> no cursor row yet, no messages persisted.
        assert read_imessage_cursor(fresh_db) is None
        assert read_messages_count(fresh_db, source="imessage") == 0

        # Inject 5 inbound rows past the fresh-install seed watermark.
        for i in range(PRE_STOP_BATCH_SIZE):
            rowid = FIRST_INJECTED_ROWID + i
            pre_stop_rowids.append(rowid)
            append_chat_db_inbound_row(
                chat_db_copy,
                rowid=rowid,
                text_value=f"cursor-recovery pre-stop {i + 1}",
            )

        max_pre_stop_rowid = pre_stop_rowids[-1]

        # Wait at least ~2 poll cycles for the cursor to advance past the
        # last injected ROWID. We poll on the cursor specifically because
        # it advances inside the same transaction as the message INSERT
        # (spec §5.1) -- if the cursor is at >= max_pre_stop_rowid the
        # row is already persisted.
        await _wait_for_cursor_at_least(
            fresh_db,
            max_pre_stop_rowid,
            timeout=DRAIN_TIMEOUT_SECONDS,
            label="pre-stop drain",
        )

        # All 5 pre-stop rows are persisted exactly once.
        assert read_messages_count(fresh_db, source="imessage") == PRE_STOP_BATCH_SIZE, (
            "pre-stop drain should yield exactly 5 imessage rows; "
            f"got {read_messages_count(fresh_db, source='imessage')}"
        )
    finally:
        # ------------------------------------------------------------------
        # Phase 2 -- stop the connector. ``stop()`` sets the stop event,
        # awaits the in-flight cycle, and closes the reader -- equivalent
        # to "await its task cancellation" per the task brief.
        # ------------------------------------------------------------------
        await connector1.stop()

    # Snapshot the persisted cursor + count after stop.
    cursor_after_stop = read_imessage_cursor(fresh_db)
    assert cursor_after_stop is not None, (
        "expected connector_state.cursor row after pre-stop drain"
    )
    assert int(cursor_after_stop) == pre_stop_rowids[-1], (
        f"expected cursor=={pre_stop_rowids[-1]} after pre-stop drain; "
        f"got {cursor_after_stop!r}"
    )
    count_after_stop = read_messages_count(fresh_db, source="imessage")
    assert count_after_stop == PRE_STOP_BATCH_SIZE

    # ------------------------------------------------------------------
    # Phase 3 -- inject 5 more rows while the connector is stopped.
    # ROWIDs strictly greater than every previous row so the
    # WHERE ROWID > cursor clause picks them up cleanly on restart.
    # ------------------------------------------------------------------
    post_stop_rowids: list[int] = []
    post_stop_start = max_pre_stop_rowid + 100  # generous gap
    for i in range(POST_STOP_BATCH_SIZE):
        rowid = post_stop_start + i
        post_stop_rowids.append(rowid)
        append_chat_db_inbound_row(
            chat_db_copy,
            rowid=rowid,
            text_value=f"cursor-recovery post-stop {i + 1}",
        )

    max_post_stop_rowid = post_stop_rowids[-1]

    # Sanity: chat.db now has every fixture row + the 10 we injected,
    # and the connector is fully stopped (no background task).
    assert _max_chat_db_rowid(chat_db_copy) == max_post_stop_rowid

    # ------------------------------------------------------------------
    # Phase 4 -- restart with a fresh connector against the SAME AMG DB
    # and chat.db copy. The persisted cursor from phase 1 is the
    # recovery trigger; the WHERE ROWID > cursor clause filters the
    # 5 already-drained rows at SQL.
    # ------------------------------------------------------------------
    reader2 = ChatDbReader(chat_db_copy)
    connector2 = ImessageConnector(
        reader=reader2,
        sender=fake_sender,
        sink=real_sink,
        allowlist=allowlist,
        session_factory=session_factory,
        attachment_store=None,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    await connector2.start()

    try:
        # Wait for the cursor to advance past the last post-stop ROWID.
        await _wait_for_cursor_at_least(
            fresh_db,
            max_post_stop_rowid,
            timeout=DRAIN_TIMEOUT_SECONDS,
            label="post-restart drain",
        )

        # ------------------------------------------------------------------
        # Phase 5 -- assertions on persisted state.
        # ------------------------------------------------------------------

        # 1. All 10 injected rows are present in `messages` exactly once.
        final_messages = list_messages_for_assertion(fresh_db)
        imessage_rows = [r for r in final_messages if r["source"] == "imessage"]
        assert len(imessage_rows) == TOTAL_INJECTED, (
            f"expected {TOTAL_INJECTED} imessage rows in messages; "
            f"got {len(imessage_rows)}: "
            f"{[r['text'] for r in imessage_rows]}"
        )

        # 2. Each injected text appears exactly once (no duplicates).
        all_injected_texts = [
            f"cursor-recovery pre-stop {i + 1}" for i in range(PRE_STOP_BATCH_SIZE)
        ] + [
            f"cursor-recovery post-stop {i + 1}" for i in range(POST_STOP_BATCH_SIZE)
        ]
        for text_body in all_injected_texts:
            matches = [r for r in imessage_rows if r["text"] == text_body]
            assert len(matches) == 1, (
                f"row {text_body!r} should appear exactly once; "
                f"found {len(matches)}: {matches}"
            )

        # 3. connector_state.cursor advanced to MAX(injected ROWID).
        final_cursor = read_imessage_cursor(fresh_db)
        assert final_cursor is not None
        assert int(final_cursor) == max_post_stop_rowid, (
            f"expected connector_state.cursor == {max_post_stop_rowid} "
            f"(MAX injected ROWID); got {final_cursor!r}"
        )
    finally:
        await connector2.stop()
