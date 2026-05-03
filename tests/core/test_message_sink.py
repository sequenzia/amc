"""Tests for ``amc.core.message_sink`` (spec §5.1, §5.5, §5.7, OQ-2).

Coverage:

Acceptance (per task #38):
* Allowlisted inbound INSERT → exactly 1 ``webhook_deliveries`` row.
* Quarantined inbound (sender not in allowlist) → 0 webhook rows.
* Outbound → 0 webhook rows.
* Webhook URL unset (``WebhookConfig`` is ``None``) → 0 webhook rows.
* All writes happen in a single SQLite transaction (rollback test).
* Concurrent connectors don't deadlock (WAL + short tx) — exercised by
  running two ``record_inbound`` coroutines in parallel against the same
  DB file.

Setup mirrors ``tests/core/test_webhook.py`` and ``tests/core/test_allowlist.py``:
tmp_path SQLite + ``alembic upgrade head``, async session factory bound to
the same path, fake clock for deterministic timestamps.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker

from amc.core.allowlist import AllowlistEntry, AllowlistLoader
from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.envelope import (
    Attachment,
    ChannelType,
    Direction,
    Envelope,
    Sender,
    Source,
)
from amc.core.message_sink import MessageSink
from amc.core.webhook import WebhookConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

WEBHOOK_URL = "https://receiver.example/hooks/amc"
WEBHOOK_SECRET = "shared-secret-1234567890"  # noqa: S105 - test fixture

ALLOWED_HANDLE = "+15551234567"
QUARANTINED_HANDLE = "+15559999999"

MSG_ID_ALLOWED = "msg_01HXYZ1234567890ABCDEFGHJK"
MSG_ID_QUARANTINED = "msg_01HXYZ1234567890ABCDEFGHJM"
MSG_ID_OUTBOUND = "msg_01HXYZ1234567890ABCDEFGHJN"
MSG_ID_DISABLED = "msg_01HXYZ1234567890ABCDEFGHJP"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeClock:
    __slots__ = ("now",)

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """tmp_path SQLite file with the full schema applied via alembic."""
    db_path = tmp_path / "sink.db"
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
        # Best-effort dispose; aiosqlite tolerates a fresh-loop close.
        import contextlib

        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture
def allowlist(tmp_path: Path) -> AllowlistLoader:
    """Loader pre-populated with a single allowed iMessage entry."""
    loader = AllowlistLoader(tmp_path / "allowlist.toml")
    # Skip file parsing — install entries directly. The loader's ``__slots__``
    # are ``path`` and ``_entries``; we set the in-memory map.
    loader._entries = {
        (Source.IMESSAGE, ALLOWED_HANDLE): AllowlistEntry(
            source=Source.IMESSAGE,
            id=ALLOWED_HANDLE,
            display_name="Alice",
            person_id="alice",
        ),
    }
    return loader


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def _build_inbound(
    *,
    message_id: str,
    sender_id: str,
    sender_display: str = "Sender",
    sender_person_id: str | None = None,
    text: str = "hello",
    timestamp: datetime | None = None,
    attachments: list[Attachment] | None = None,
) -> Envelope:
    return Envelope(
        id=message_id,
        source=Source.IMESSAGE,
        channel_id=sender_id,  # iMessage DM: channel id == sender id
        channel_type=ChannelType.DM,
        sender=Sender(
            id=sender_id,
            display_name=sender_display,
            person_id=sender_person_id,
        ),
        text=text,
        attachments=attachments or [],
        reply_to=None,
        timestamp=timestamp or datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC),
        direction=Direction.INBOUND,
        raw={},
    )


def _build_outbound(
    *,
    message_id: str = MSG_ID_OUTBOUND,
    channel_id: str = ALLOWED_HANDLE,
    text: str = "reply",
    timestamp: datetime | None = None,
) -> Envelope:
    return Envelope(
        id=message_id,
        source=Source.IMESSAGE,
        channel_id=channel_id,
        channel_type=ChannelType.DM,
        sender=Sender(id="agent", display_name="Agent", person_id=None),
        text=text,
        attachments=[],
        reply_to=None,
        timestamp=timestamp or datetime(2026, 5, 3, 12, 0, 5, tzinfo=UTC),
        direction=Direction.OUTBOUND,
        raw={},
    )


def _list_deliveries(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM webhook_deliveries ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _list_messages(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _read_row(db_path: Path, sql: str, params: tuple = ()) -> dict | None:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Acceptance: webhook gating
# ---------------------------------------------------------------------------


async def test_allowlisted_inbound_enqueues_exactly_one_webhook_row(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """AC: Allowlisted inbound INSERT → exactly 1 ``webhook_deliveries`` row."""
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET),
        time_provider=clock,
    )
    envelope = _build_inbound(
        message_id=MSG_ID_ALLOWED,
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice",
        sender_person_id="alice",
    )

    msg_id = await sink.record_inbound(envelope, source="cursor-rowid-42")

    assert msg_id == MSG_ID_ALLOWED
    deliveries = _list_deliveries(fresh_db)
    assert len(deliveries) == 1
    row = deliveries[0]
    assert row["status"] == "pending"
    assert row["attempt"] == 0
    assert row["message_id"] == MSG_ID_ALLOWED
    assert row["next_retry_at"] is not None


async def test_quarantined_inbound_creates_zero_webhook_rows(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """AC: Quarantined inbound (sender not allowlisted) → 0 webhook rows."""
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET),
        time_provider=clock,
    )
    envelope = _build_inbound(
        message_id=MSG_ID_QUARANTINED,
        sender_id=QUARANTINED_HANDLE,
        sender_display="Stranger",
    )

    await sink.record_inbound(envelope, source="cursor-rowid-43")

    # Message persisted with allowlist_status='unknown'.
    msg = _read_row(fresh_db, "SELECT * FROM messages WHERE id = ?", (MSG_ID_QUARANTINED,))
    assert msg is not None
    assert msg["allowlist_status"] == "unknown"
    # No webhook row.
    assert _list_deliveries(fresh_db) == []


async def test_outbound_creates_zero_webhook_rows(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """AC: Outbound → 0 webhook rows (OQ-2 v1 default)."""
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET),
        time_provider=clock,
    )
    envelope = _build_outbound()

    await sink.record_outbound(envelope, source="msg_id_we_just_sent")

    msg = _read_row(fresh_db, "SELECT * FROM messages WHERE id = ?", (MSG_ID_OUTBOUND,))
    assert msg is not None
    assert msg["direction"] == "outbound"
    assert msg["allowlist_status"] == "outbound"
    assert _list_deliveries(fresh_db) == []


async def test_webhook_url_unset_creates_zero_rows_even_for_allowed_inbound(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """AC: ``AMC_WEBHOOK_URL`` unset (``webhook_config=None``) → no rows."""
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=None,  # disabled
        time_provider=clock,
    )
    envelope = _build_inbound(
        message_id=MSG_ID_DISABLED,
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice",
        sender_person_id="alice",
    )

    await sink.record_inbound(envelope, source="cursor-rowid-44")

    # Message persisted as 'allowed' but no webhook fired.
    msg = _read_row(fresh_db, "SELECT * FROM messages WHERE id = ?", (MSG_ID_DISABLED,))
    assert msg is not None
    assert msg["allowlist_status"] == "allowed"
    assert _list_deliveries(fresh_db) == []


# ---------------------------------------------------------------------------
# Acceptance: single transaction
# ---------------------------------------------------------------------------


async def test_all_writes_persisted_atomically(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """AC: All writes happen in a single SQLite transaction.

    Verifies the success path: after one ``record_inbound`` call, every
    table that should have been touched has its row, and the ``messages``,
    ``senders``, ``channels``, ``connector_state``, and ``webhook_deliveries``
    rows all share consistent ``source``/``message_id`` linkage.
    """
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET),
        time_provider=clock,
    )
    att = Attachment(
        id="att_01HXYZ1234567890ABCDEFGHJK",
        url="https://cdn.example/foo.png",
        mime="image/png",
        size_bytes=42,
    )
    envelope = _build_inbound(
        message_id=MSG_ID_ALLOWED,
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice",
        sender_person_id="alice",
        attachments=[att],
    )

    await sink.record_inbound(envelope, source="cursor-rowid-100")

    msg = _read_row(fresh_db, "SELECT * FROM messages WHERE id = ?", (MSG_ID_ALLOWED,))
    sender = _read_row(
        fresh_db,
        "SELECT * FROM senders WHERE source = ? AND sender_id = ?",
        ("imessage", ALLOWED_HANDLE),
    )
    channel = _read_row(
        fresh_db,
        "SELECT * FROM channels WHERE source = ? AND channel_id = ?",
        ("imessage", ALLOWED_HANDLE),
    )
    cursor_row = _read_row(
        fresh_db, "SELECT * FROM connector_state WHERE source = ?", ("imessage",)
    )
    attachment_row = _read_row(
        fresh_db, "SELECT * FROM attachments WHERE id = ?", (att.id,)
    )
    deliveries = _list_deliveries(fresh_db)

    assert msg is not None
    assert sender is not None
    assert channel is not None
    assert cursor_row is not None
    assert cursor_row["cursor"] == "cursor-rowid-100"
    assert attachment_row is not None
    assert attachment_row["message_id"] == MSG_ID_ALLOWED
    assert len(deliveries) == 1
    assert deliveries[0]["message_id"] == MSG_ID_ALLOWED


async def test_failure_rolls_back_all_writes(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """AC: Single transaction — a duplicate-id INSERT must roll back EVERY
    write in the call (no partial messages/senders/channels/cursor/webhook).

    Strategy: persist message #1 successfully, then attempt to persist
    message #2 with the **same id** but a different cursor and attachment.
    The INSERT into messages must raise; the surrounding tx must roll back
    so the cursor stays at message #1's value and the new attachment row
    is never created.
    """
    from sqlalchemy.exc import IntegrityError

    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET),
        time_provider=clock,
    )
    envelope1 = _build_inbound(
        message_id=MSG_ID_ALLOWED,
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice",
        sender_person_id="alice",
    )
    await sink.record_inbound(envelope1, source="cursor-1")

    # Sanity: cursor is at "cursor-1", one delivery row exists.
    cursor_before = _read_row(
        fresh_db, "SELECT cursor FROM connector_state WHERE source = ?", ("imessage",)
    )
    assert cursor_before is not None
    assert cursor_before["cursor"] == "cursor-1"
    assert len(_list_deliveries(fresh_db)) == 1

    # Now try to record a SECOND message with the same id — the INSERT into
    # messages will fail PRIMARY KEY uniqueness, raising IntegrityError.
    new_attachment = Attachment(
        id="att_01HXYZ9999999999999999999A",
        url="https://cdn.example/bar.png",
        mime="image/png",
        size_bytes=99,
    )
    envelope_dup = _build_inbound(
        message_id=MSG_ID_ALLOWED,  # SAME id → conflict
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice (dup)",
        sender_person_id="alice",
        attachments=[new_attachment],
    )

    with pytest.raises(IntegrityError):
        await sink.record_inbound(envelope_dup, source="cursor-2")

    # Cursor must NOT have advanced.
    cursor_after = _read_row(
        fresh_db, "SELECT cursor FROM connector_state WHERE source = ?", ("imessage",)
    )
    assert cursor_after is not None
    assert cursor_after["cursor"] == "cursor-1"

    # No new attachment row.
    new_att_row = _read_row(
        fresh_db, "SELECT * FROM attachments WHERE id = ?", (new_attachment.id,)
    )
    assert new_att_row is None

    # Still exactly one delivery row from the first (successful) INSERT.
    assert len(_list_deliveries(fresh_db)) == 1


# ---------------------------------------------------------------------------
# Acceptance: concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_record_inbound_does_not_deadlock(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """AC: Concurrent connectors don't deadlock (WAL + short tx).

    Run two ``record_inbound`` coroutines in parallel against the same DB
    file with two distinct messages. Both must complete within a generous
    timeout (3 s — well above the WAL writer-lock contention window).
    """
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET),
        time_provider=clock,
    )
    msg_a_id = "msg_01HXYZ1234567890ABCDEFGHJA"
    msg_b_id = "msg_01HXYZ1234567890ABCDEFGHJB"
    env_a = _build_inbound(
        message_id=msg_a_id,
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice",
        sender_person_id="alice",
        text="msg A",
    )
    env_b = _build_inbound(
        message_id=msg_b_id,
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice",
        sender_person_id="alice",
        text="msg B",
    )

    await asyncio.wait_for(
        asyncio.gather(
            sink.record_inbound(env_a, source="cursor-A"),
            sink.record_inbound(env_b, source="cursor-B"),
        ),
        timeout=3.0,
    )

    msgs = _list_messages(fresh_db)
    assert {m["id"] for m in msgs} == {msg_a_id, msg_b_id}
    deliveries = _list_deliveries(fresh_db)
    assert len(deliveries) == 2


# ---------------------------------------------------------------------------
# Direction guards
# ---------------------------------------------------------------------------


async def test_record_inbound_rejects_outbound_envelope(
    session_factory, allowlist: AllowlistLoader, clock: _FakeClock
):
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=None,
        time_provider=clock,
    )
    envelope = _build_outbound()
    with pytest.raises(ValueError, match="record_inbound"):
        await sink.record_inbound(envelope, source="x")


async def test_record_outbound_rejects_inbound_envelope(
    session_factory, allowlist: AllowlistLoader, clock: _FakeClock
):
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=None,
        time_provider=clock,
    )
    envelope = _build_inbound(
        message_id=MSG_ID_ALLOWED,
        sender_id=ALLOWED_HANDLE,
    )
    with pytest.raises(ValueError, match="record_outbound"):
        await sink.record_outbound(envelope, source="x")


# ---------------------------------------------------------------------------
# Snapshot semantics
# ---------------------------------------------------------------------------


async def test_url_snapshot_is_populated_on_enqueue(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """The sink writes the per-delivery URL into the supplied snapshot map.

    Verifies the integration with :func:`amc.core.webhook.enqueue_webhook` —
    once a row is enqueued, ``snapshot[delivery_id]`` must equal the
    configured URL so an in-flight retry uses the URL captured at queue
    time (spec §5.5 edge-case row "Webhook URL changes at runtime").
    """
    snapshot: dict[str, str] = {}
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=WebhookConfig(url=WEBHOOK_URL, secret=WEBHOOK_SECRET),
        url_snapshot=snapshot,
        time_provider=clock,
    )
    envelope = _build_inbound(
        message_id=MSG_ID_ALLOWED,
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice",
        sender_person_id="alice",
    )

    await sink.record_inbound(envelope, source="cursor-x")

    deliveries = _list_deliveries(fresh_db)
    assert len(deliveries) == 1
    delivery_id = deliveries[0]["id"]
    assert snapshot.get(delivery_id) == WEBHOOK_URL


async def test_attachments_json_persisted_on_messages_row(
    session_factory, fresh_db: Path, allowlist: AllowlistLoader, clock: _FakeClock
):
    """``messages.attachments_json`` is a denormalized JSON snapshot."""
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=None,  # webhook irrelevant here
        time_provider=clock,
    )
    att_id = "att_01HXYZ1234567890ABCDEFGHJM"
    att = Attachment(
        id=att_id,
        url="https://cdn.example/a.png",
        mime="image/png",
        size_bytes=10,
    )
    envelope = _build_inbound(
        message_id=MSG_ID_ALLOWED,
        sender_id=ALLOWED_HANDLE,
        sender_display="Alice",
        sender_person_id="alice",
        attachments=[att],
    )

    await sink.record_inbound(envelope, source="c")

    msg = _read_row(fresh_db, "SELECT * FROM messages WHERE id = ?", (MSG_ID_ALLOWED,))
    assert msg is not None
    assert msg["attachments_json"] is not None
    import json as _json

    decoded = _json.loads(msg["attachments_json"])
    assert decoded == [
        {
            "id": att_id,
            "url": "https://cdn.example/a.png",
            "mime": "image/png",
            "size_bytes": 10,
        }
    ]


async def test_returns_envelope_id(
    session_factory, allowlist: AllowlistLoader, clock: _FakeClock
):
    """Both methods return ``envelope.id`` for caller convenience."""
    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=None,
        time_provider=clock,
    )
    inbound = _build_inbound(message_id=MSG_ID_ALLOWED, sender_id=ALLOWED_HANDLE)
    outbound = _build_outbound(message_id=MSG_ID_OUTBOUND)

    in_id = await sink.record_inbound(inbound, source="c1")
    out_id = await sink.record_outbound(outbound, source="c2")

    assert in_id == MSG_ID_ALLOWED
    assert out_id == MSG_ID_OUTBOUND
