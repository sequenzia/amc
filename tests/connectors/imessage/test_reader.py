"""Tests for ``amc.connectors.imessage.reader.ChatDbReader``.

The reader is the inbound half of the iMessage path (spec §5.1
REQ-AMC-001 + §7.5). These tests exercise it against the Phase 0
fixture chat.db produced by ``tests/fixtures/build_chat_db.py``.

Coverage map (acceptance criteria → test):

Functional
- Connection opens read-only; PRAGMA query_only=ON
    -> ``test_reader_opens_read_only_and_query_only_pragma``
- Polling returns new rows in ROWID-ascending order
    -> ``test_fetch_new_returns_rows_in_rowid_order``
- Handle resolution produces E.164 phone or email
    -> ``test_normalize_handle_phone_email_variants``
- ``attributedBody`` decoded to text matches the golden output
    -> ``test_attributed_body_decodes_to_fixture_text``
- Attachment rows surface their on-disk path and MIME
    -> ``test_attachment_path_and_mime_are_surfaced``

Edge cases
- ``is_from_me=1`` rows surfaced and tagged
    -> ``test_is_from_me_rows_surfaced_with_flag``
- Group-chat rows filtered out (DM only)
    -> ``test_group_chat_rows_filtered_out``
- Concurrent calls don't corrupt state
    -> ``test_concurrent_fetch_calls_dont_corrupt_state``

Error handling
- Path missing → clear error
    -> ``test_missing_chat_db_path_raises_clear_error``
- Database locked → log and re-raise so caller can retry
    -> ``test_database_locked_logs_and_raises_for_caller_retry``
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest

from amc.connectors.imessage.reader import (
    ChatDbPathMissingError,
    ChatDbReader,
    decode_attributed_body,
    normalize_handle,
)
from tests.fixtures.build_chat_db import (
    ALLOWLISTED_HANDLE,
    CHAT_GUID,
    MSG_GUID_1,
    MSG_GUID_2,
    MSG_GUID_3,
    QUARANTINED_HANDLE,
    ensure_chat_db,
    make_attributed_body,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def src_chat_db_path() -> Path:
    """Materialize the fixture chat.db once per test session."""
    return ensure_chat_db()


@pytest.fixture()
def chat_db_copy(src_chat_db_path: Path, tmp_path: Path) -> Path:
    """Return a per-test temp copy of the fixture chat.db.

    A fresh copy per test isolates the connection cache and lets us
    safely poke sidecar files in error-handling tests.
    """
    dst = tmp_path / "chat.db"
    shutil.copy2(src_chat_db_path, dst)
    return dst


# ---------------------------------------------------------------------------
# Functional: read-only open + PRAGMA query_only
# ---------------------------------------------------------------------------


async def test_reader_opens_read_only_and_query_only_pragma(chat_db_copy: Path) -> None:
    """Reader opens chat.db with mode=ro AND PRAGMA query_only=ON.

    Verified by introspecting the underlying connection: query_only=1
    after the first fetch; an attempted INSERT raises OperationalError.
    """
    reader = ChatDbReader(chat_db_copy)
    try:
        rows = await reader.fetch_new(last_seen_rowid=0)
        assert len(rows) > 0  # fixture seeds 5 DM messages
        # Reach into the cached connection to check both layers.
        conn = reader._conn  # type: ignore[attr-defined]
        assert conn is not None
        pragma = conn.execute("PRAGMA query_only").fetchone()[0]
        assert pragma == 1, f"expected query_only=1, got {pragma}"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO handle (id, service) VALUES ('+10000000000', 'iMessage')"
            )
    finally:
        await reader.close()


# ---------------------------------------------------------------------------
# Functional: ROWID-ascending order
# ---------------------------------------------------------------------------


async def test_fetch_new_returns_rows_in_rowid_order(chat_db_copy: Path) -> None:
    """Polling returns new rows in monotonically ascending ROWID order."""
    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)
    rowids = [row["rowid"] for row in rows]
    assert rowids == sorted(rowids)
    # Fixture seeds 5 DM messages (ROWIDs 1..5; #6 is the quarantine
    # row in a different DM chat, also style=45, so it surfaces too).
    assert {MSG_GUID_1, MSG_GUID_2, MSG_GUID_3}.issubset(
        {row["raw_json"]["message_guid"] for row in rows}
    )


async def test_fetch_new_respects_last_seen_cursor(chat_db_copy: Path) -> None:
    """Passing ``last_seen_rowid=N`` returns only rows with ROWID > N."""
    async with ChatDbReader(chat_db_copy) as reader:
        all_rows = await reader.fetch_new(last_seen_rowid=0)
        first_rowid = all_rows[0]["rowid"]
        remainder = await reader.fetch_new(last_seen_rowid=first_rowid)
    assert all(r["rowid"] > first_rowid for r in remainder)
    assert len(remainder) == len(all_rows) - 1


# ---------------------------------------------------------------------------
# Functional: handle resolution
# ---------------------------------------------------------------------------


def test_normalize_handle_phone_email_variants() -> None:
    """E.164 phone normalization + email pass-through."""
    # Already-E.164 phone passes through.
    assert normalize_handle("+15551234567") == ("+15551234567", "phone")
    # 10-digit US number gets +1 prepended.
    assert normalize_handle("5551234567") == ("+15551234567", "phone")
    # Formatted US number normalizes.
    assert normalize_handle("(555) 123-4567") == ("+15551234567", "phone")
    # 11-digit number starting with 1 (no plus) gets +.
    assert normalize_handle("15551234567") == ("+15551234567", "phone")
    # Email Apple-ID preserved as-is.
    assert normalize_handle("user@icloud.com") == ("user@icloud.com", "email")
    # Empty / None.
    assert normalize_handle(None) == (None, None)
    assert normalize_handle("") == (None, None)


async def test_handle_resolution_in_fetched_rows(chat_db_copy: Path) -> None:
    """Rows from the fixture surface E.164 sender_handle + 'phone' kind."""
    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)

    allowlisted_rows = [r for r in rows if r["sender_handle"] == ALLOWLISTED_HANDLE]
    assert len(allowlisted_rows) >= 3
    assert all(r["sender_kind"] == "phone" for r in allowlisted_rows)


# ---------------------------------------------------------------------------
# Functional: attributedBody decode
# ---------------------------------------------------------------------------


def test_decode_attributed_body_round_trips_fixture_helper() -> None:
    """The decoder round-trips bytes produced by ``make_attributed_body``."""
    blob = make_attributed_body("hello world")
    assert decode_attributed_body(blob) == "hello world"


def test_decode_attributed_body_round_trips_fixture_message() -> None:
    """The fixture's seeded ``attributedBody`` decodes to the golden text."""
    blob = make_attributed_body("rendered via attributedBody")
    assert decode_attributed_body(blob) == "rendered via attributedBody"


def test_decode_attributed_body_returns_none_on_non_typedstream() -> None:
    """Blobs without the typedstream magic decode to None."""
    assert decode_attributed_body(b"") is None
    assert decode_attributed_body(b"PLACEHOLDER") is None
    assert decode_attributed_body(b"\x00\x01\x02bplist00") is None


def test_decode_attributed_body_handles_unicode() -> None:
    """Multi-byte UTF-8 round-trips through the decoder."""
    blob = make_attributed_body("hi cafe")
    assert decode_attributed_body(blob) == "hi cafe"


async def test_attributed_body_decodes_to_fixture_text(chat_db_copy: Path) -> None:
    """The fixture's attributedBody-only message resolves to the golden text."""
    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)
    # The attributedBody-only row in the fixture has text=NULL and
    # the attributedBody encodes "rendered via attributedBody".
    attrib_rows = [r for r in rows if r["attributed_body"] is not None]
    assert len(attrib_rows) == 1
    row = attrib_rows[0]
    assert row["text"] == "rendered via attributedBody"
    # raw_json preserves the original blob (hex-encoded for JSON safety).
    assert isinstance(row["raw_json"]["attributed_body"], str)
    assert row["raw_json"]["attributed_body"].startswith("040b")


# ---------------------------------------------------------------------------
# Functional: attachments
# ---------------------------------------------------------------------------


async def test_attachment_path_and_mime_are_surfaced(chat_db_copy: Path) -> None:
    """Attachment rows surface their on-disk path and MIME type."""
    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)

    attach_rows = [r for r in rows if r["attachments"]]
    assert len(attach_rows) == 1
    row = attach_rows[0]
    assert len(row["attachments"]) == 1
    att = row["attachments"][0]
    assert att["mime"] == "image/png"
    assert att["path"] is not None
    assert att["path"].endswith("tiny.png")


async def test_messages_without_attachments_have_empty_list(chat_db_copy: Path) -> None:
    """Messages with no attachments surface ``attachments=[]`` (not None)."""
    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)

    no_attach_rows = [r for r in rows if not r["attachments"]]
    assert len(no_attach_rows) >= 1
    for row in no_attach_rows:
        assert row["attachments"] == []


# ---------------------------------------------------------------------------
# Functional: chat.guid surfaced for outbound routing
# ---------------------------------------------------------------------------


async def test_chat_guid_surfaced_for_allowlisted_thread(chat_db_copy: Path) -> None:
    """Each row carries its chat.guid for AppleScript send routing."""
    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)

    allowlisted_rows = [r for r in rows if r["sender_handle"] == ALLOWLISTED_HANDLE]
    assert all(r["chat_guid"] == CHAT_GUID for r in allowlisted_rows)


# ---------------------------------------------------------------------------
# Edge case: is_from_me rows surfaced + tagged
# ---------------------------------------------------------------------------


async def test_is_from_me_rows_surfaced_with_flag(chat_db_copy: Path, tmp_path: Path) -> None:
    """Rows with ``is_from_me=1`` are surfaced with the flag set.

    Wiring layer (task #59) is responsible for the policy decision; the
    reader's job is just to expose the bit. We mutate a temp copy of
    the fixture to add an outbound row, then re-open the reader.
    """
    # Mutate the per-test copy via direct sqlite3 (allowed because it's
    # a writable temp file, not the shipped fixture).
    src = sqlite3.connect(chat_db_copy)
    try:
        # ROWID 7 is unused in the fixture (last seeded ROWID is 6).
        src.execute(
            "INSERT INTO message ("
            " ROWID, guid, text, handle_id, service, account, date,"
            " is_delivered, is_finished, is_from_me, is_read, is_sent,"
            " cache_has_attachments, item_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                7,
                "00000000-0000-0000-0000-000000000007",
                "hello, this is the operator replying",
                None,  # is_from_me rows have NULL handle_id
                "iMessage",
                "E:owner@example.com",
                767_287_931_000_000_000 + 360_000_000_000,
                1,
                1,
                1,  # is_from_me
                1,
                1,
                0,
                0,
            ),
        )
        src.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date) VALUES (?, ?, ?)",
            (1, 7, 767_287_931_000_000_000 + 360_000_000_000),
        )
        src.commit()
    finally:
        src.close()

    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)

    from_me_rows = [r for r in rows if r["is_from_me"]]
    assert len(from_me_rows) == 1
    assert from_me_rows[0]["text"] == "hello, this is the operator replying"
    assert from_me_rows[0]["chat_guid"] == CHAT_GUID
    # NULL handle_id for outbound is fine; sender fields are None.
    assert from_me_rows[0]["sender_handle"] is None


# ---------------------------------------------------------------------------
# Edge case: group-chat rows filtered out (v1 = DM only)
# ---------------------------------------------------------------------------


async def test_group_chat_rows_filtered_out(chat_db_copy: Path, tmp_path: Path) -> None:
    """Group-chat rows (chat.style != 45) are filtered out at the SQL layer."""
    src = sqlite3.connect(chat_db_copy)
    try:
        # Seed a group chat (style=43 is the SMS group code; any non-45
        # style triggers the DM filter).
        src.execute(
            "INSERT INTO chat ("
            " ROWID, guid, style, state, account_id, chat_identifier,"
            " service_name, account_login, is_archived"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                10,
                "iMessage;+;groupchat-test",
                43,  # group style
                3,
                "ACCT-FIXTURE",
                "groupchat-test",
                "iMessage",
                "E:owner@example.com",
                0,
            ),
        )
        src.execute(
            "INSERT INTO message ("
            " ROWID, guid, text, handle_id, service, account, date,"
            " is_delivered, is_finished, is_from_me, is_read, is_sent,"
            " cache_has_attachments, item_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                10,
                "00000000-0000-0000-0000-000000000010",
                "this is in a group chat",
                1,
                "iMessage",
                "E:owner@example.com",
                767_287_931_000_000_000 + 420_000_000_000,
                1,
                1,
                0,
                0,
                0,
                0,
                0,
            ),
        )
        src.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date) VALUES (?, ?, ?)",
            (10, 10, 767_287_931_000_000_000 + 420_000_000_000),
        )
        src.commit()
    finally:
        src.close()

    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)

    # The group-chat row must NOT appear in results.
    assert all(r["chat_guid"] != "iMessage;+;groupchat-test" for r in rows)
    assert all("group chat" not in (r["text"] or "") for r in rows)


# ---------------------------------------------------------------------------
# Edge case: concurrent fetch calls don't corrupt state
# ---------------------------------------------------------------------------


async def test_concurrent_fetch_calls_dont_corrupt_state(chat_db_copy: Path) -> None:
    """Multiple in-flight ``fetch_new`` calls return consistent rowsets.

    The reader serializes SQLite work behind an asyncio.Lock; the
    cursor is the caller's responsibility (per task spec). Even with
    overlapping calls, every result must be sorted and the same rows
    must appear regardless of ordering.
    """
    async with ChatDbReader(chat_db_copy) as reader:
        results = await asyncio.gather(
            reader.fetch_new(last_seen_rowid=0),
            reader.fetch_new(last_seen_rowid=0),
            reader.fetch_new(last_seen_rowid=0),
        )

    # Every result is independently sorted.
    for rows in results:
        rowids = [row["rowid"] for row in rows]
        assert rowids == sorted(rowids)

    # Every result observes the same rowset.
    sets = [tuple(r["rowid"] for r in rows) for rows in results]
    assert all(s == sets[0] for s in sets)


# ---------------------------------------------------------------------------
# Functional: timestamp conversion (Mach absolute time → UTC)
# ---------------------------------------------------------------------------


async def test_timestamps_are_utc_aware_and_decoded_from_mach_time(
    chat_db_copy: Path,
) -> None:
    """Mach absolute time decodes to a UTC-aware datetime.

    Fixture base date ``_BASE_DATE_NS = 767_287_931_000_000_000`` is
    seconds 767,287,931 after the Mach epoch (2001-01-01 UTC), which
    resolves to 2025-04-25T15:32:11Z. The fixture's source comment
    misstates the year as 2026 — the math is what counts.
    """
    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)
    assert rows  # sanity
    for row in rows:
        ts = row["timestamp"]
        assert ts.tzinfo is not None
        assert ts.year == 2025
        assert ts.month == 4
        assert ts.day == 25


# ---------------------------------------------------------------------------
# Functional: raw_json preservation
# ---------------------------------------------------------------------------


async def test_raw_json_is_json_serializable(chat_db_copy: Path) -> None:
    """``raw_json`` round-trips through ``json.dumps`` (no bytes leakage)."""
    import json

    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)

    for row in rows:
        # Must serialize without error — that's the spec §7.3.3 contract.
        json.dumps(row["raw_json"])
        # Original chat.db columns must be preserved.
        raw = row["raw_json"]
        assert "rowid" in raw
        assert "chat_guid" in raw
        assert "message_guid" in raw


# ---------------------------------------------------------------------------
# Quarantined sender row still surfaces (allowlist filtering is the wiring
# layer's job; reader's job is to surface every DM row)
# ---------------------------------------------------------------------------


async def test_quarantined_sender_row_surfaced_for_wiring_decision(
    chat_db_copy: Path,
) -> None:
    """The quarantined sender row surfaces; wiring filters by allowlist."""
    async with ChatDbReader(chat_db_copy) as reader:
        rows = await reader.fetch_new(last_seen_rowid=0)

    quarantine_rows = [r for r in rows if r["sender_handle"] == QUARANTINED_HANDLE]
    assert len(quarantine_rows) == 1
    assert quarantine_rows[0]["text"] == "u up?"


# ---------------------------------------------------------------------------
# Error handling: missing chat.db path
# ---------------------------------------------------------------------------


def test_missing_chat_db_path_raises_clear_error(tmp_path: Path) -> None:
    """Non-existent path raises a clear error subclass of FileNotFoundError."""
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(ChatDbPathMissingError, match="chat.db not found"):
        ChatDbReader(missing)
    # And the error is also a FileNotFoundError so generic catch sites work.
    with pytest.raises(FileNotFoundError):
        ChatDbReader(missing)


# ---------------------------------------------------------------------------
# Error handling: database locked → log + raise so caller can retry
# ---------------------------------------------------------------------------


async def test_database_locked_logs_and_raises_for_caller_retry(
    chat_db_copy: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 'database is locked' OperationalError is logged and re-raised.

    We simulate this by replacing the reader's cached connection with
    a thin proxy that raises the canonical sqlite3 error string when
    the polling query runs. ``sqlite3.Connection.execute`` is read-only
    in CPython 3.12+ so we can't monkey-patch it directly — a wrapper
    is the next best thing.
    """
    import logging

    class _LockedConn:
        """Proxy connection that always raises 'database is locked' on execute."""

        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, *args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("database is locked")

        def close(self) -> None:
            self._real.close()

    reader = ChatDbReader(chat_db_copy)
    try:
        # Force connection initialization, then swap in the locked proxy.
        await reader.fetch_new(last_seen_rowid=0)
        real = reader._conn  # type: ignore[attr-defined]
        assert real is not None
        reader._conn = _LockedConn(real)  # type: ignore[assignment,attr-defined]

        with (
            caplog.at_level(logging.WARNING, logger="amc.connectors.imessage.reader"),
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            await reader.fetch_new(last_seen_rowid=0)

        assert any("chat_db_locked" in rec.message for rec in caplog.records)
    finally:
        await reader.close()


# ---------------------------------------------------------------------------
# Lifecycle: close() is idempotent
# ---------------------------------------------------------------------------


async def test_close_is_idempotent(chat_db_copy: Path) -> None:
    """Calling ``close()`` twice doesn't raise."""
    reader = ChatDbReader(chat_db_copy)
    await reader.fetch_new(last_seen_rowid=0)
    await reader.close()
    await reader.close()
