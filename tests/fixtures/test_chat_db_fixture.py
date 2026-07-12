"""Tests for the chat.db fixture builder.

These tests confirm that:

* The builder produces a SQLite database that can be opened in read-only
  mode using a stdlib ``sqlite3`` URI (mirroring how the iMessage connector
  will open the real ``~/Library/Messages/chat.db``).
* All four seeded scenarios (DM thread, attachment, attributedBody-only,
  quarantined sender) are queryable.
* The build is deterministic — running it twice produces a byte-identical
  file.
* The ``attributedBody`` blob is a real Apple typedstream archive, not a
  placeholder.
* The builder fails fast with a clear error if a referenced attachment
  payload is missing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from tests.fixtures import build_chat_db
from tests.fixtures.build_chat_db import (
    ALLOWLISTED_HANDLE,
    ATTACHMENT_FILENAME,
    ATTACHMENTS_DIR,
    CHAT_GUID,
    MSG_GUID_1,
    MSG_GUID_2,
    MSG_GUID_3,
    MSG_GUID_ATTACH,
    MSG_GUID_ATTRIB,
    MSG_GUID_QUARANTINE,
    QUARANTINED_HANDLE,
    build,
    ensure_chat_db,
    make_attributed_body,
)


@pytest.fixture(scope="session")
def chat_db_path() -> Path:
    """Materialize the fixture chat.db once per test session."""
    return ensure_chat_db()


@pytest.fixture()
def ro_conn(chat_db_path: Path):
    """Open the fixture in read-only mode the same way the connector will.

    Mirrors spec §6.1 / §9.2: ``mode=ro`` URI plus ``PRAGMA query_only=ON``.
    """
    uri = f"file:{chat_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Functional: read-only open + seeded rows
# ---------------------------------------------------------------------------


def test_opens_read_only(ro_conn: sqlite3.Connection) -> None:
    """The DB loads via stdlib sqlite3 in mode=ro + query_only."""
    # query_only=ON should reject writes.
    with pytest.raises(sqlite3.OperationalError):
        ro_conn.execute("INSERT INTO handle (id, service) VALUES ('+10000000000', 'iMessage')")


def test_required_tables_exist(ro_conn: sqlite3.Connection) -> None:
    rows = ro_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    expected = {
        "handle",
        "chat",
        "chat_handle_join",
        "message",
        "chat_message_join",
        "attachment",
        "message_attachment_join",
    }
    assert expected.issubset(names), f"missing tables: {expected - names}"


def test_dm_thread_has_three_messages(ro_conn: sqlite3.Connection) -> None:
    rows = ro_conn.execute(
        """
        SELECT m.guid, m.text, h.id AS handle_id, c.guid AS chat_guid
        FROM message m
        JOIN handle h ON h.ROWID = m.handle_id
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE c.guid = ? AND h.id = ? AND m.text IS NOT NULL
              AND m.guid IN (?, ?, ?)
        ORDER BY m.date ASC
        """,
        (CHAT_GUID, ALLOWLISTED_HANDLE, MSG_GUID_1, MSG_GUID_2, MSG_GUID_3),
    ).fetchall()

    assert len(rows) == 3
    assert [r["guid"] for r in rows] == [MSG_GUID_1, MSG_GUID_2, MSG_GUID_3]
    assert all(r["chat_guid"] == CHAT_GUID for r in rows)
    assert all(r["handle_id"] == ALLOWLISTED_HANDLE for r in rows)


def test_attachment_row_links_to_real_file(ro_conn: sqlite3.Connection) -> None:
    row = ro_conn.execute(
        """
        SELECT a.filename, a.mime_type, a.total_bytes, a.uti, m.guid AS msg_guid
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        JOIN message m ON m.ROWID = maj.message_id
        WHERE m.guid = ?
        """,
        (MSG_GUID_ATTACH,),
    ).fetchone()

    assert row is not None
    assert row["mime_type"] == "image/png"
    assert row["uti"] == "public.png"
    # ``attachment.filename`` holds the absolute path of whichever checkout
    # built the committed fixture, so it is resolved by basename against this
    # checkout's ATTACHMENTS_DIR rather than trusted verbatim.
    assert Path(row["filename"]).name == ATTACHMENT_FILENAME
    on_disk = ATTACHMENTS_DIR / ATTACHMENT_FILENAME
    assert on_disk.exists(), f"attachment file missing: {on_disk}"
    assert on_disk.stat().st_size == row["total_bytes"]


def test_attributed_body_message_is_text_null(ro_conn: sqlite3.Connection) -> None:
    row = ro_conn.execute(
        "SELECT text, attributedBody FROM message WHERE guid = ?",
        (MSG_GUID_ATTRIB,),
    ).fetchone()

    assert row is not None
    assert row["text"] is None
    body = row["attributedBody"]
    assert isinstance(body, bytes)
    assert len(body) > 0


def test_quarantine_message_from_non_allowlisted_handle(
    ro_conn: sqlite3.Connection,
) -> None:
    row = ro_conn.execute(
        """
        SELECT m.guid, m.text, h.id AS handle_id
        FROM message m
        JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.guid = ?
        """,
        (MSG_GUID_QUARANTINE,),
    ).fetchone()

    assert row is not None
    assert row["handle_id"] == QUARANTINED_HANDLE
    assert row["handle_id"] != ALLOWLISTED_HANDLE


# ---------------------------------------------------------------------------
# Edge case: attributedBody is a real typedstream archive, not a placeholder
# ---------------------------------------------------------------------------


def test_attributed_body_is_real_typedstream(ro_conn: sqlite3.Connection) -> None:
    """The blob must start with the Apple typedstream magic (\\x04\\x0Bstreamtyped)
    and contain the NSAttributedString class marker — not a placeholder
    string like ``b"PLACEHOLDER"``.
    """
    row = ro_conn.execute(
        "SELECT attributedBody FROM message WHERE guid = ?",
        (MSG_GUID_ATTRIB,),
    ).fetchone()
    body: bytes = row["attributedBody"]

    # Magic: 0x04 0x0B then ASCII "streamtyped"
    assert body[:13] == b"\x04\x0bstreamtyped", (
        f"attributedBody missing typedstream magic; first 13 bytes = {body[:13]!r}"
    )
    # Class hierarchy marker must be present.
    assert b"NSAttributedString" in body
    assert b"NSString" in body
    # The text we encoded must be present verbatim in UTF-8.
    assert b"rendered via attributedBody" in body


def test_make_attributed_body_round_trip_byte_identical() -> None:
    """Calling the builder twice with the same input yields identical bytes."""
    a = make_attributed_body("hello")
    b = make_attributed_body("hello")
    assert a == b
    assert a[:13] == b"\x04\x0bstreamtyped"


def test_make_attributed_body_rejects_long_strings() -> None:
    with pytest.raises(ValueError, match="short strings"):
        make_attributed_body("x" * 200)


# ---------------------------------------------------------------------------
# Determinism: rebuild produces a byte-identical file
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_build_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two consecutive builds produce a byte-identical chat.db.

    The builds are redirected into ``tmp_path``. ``tests/fixtures/chat.db`` is
    a *tracked* binary, and ``build()`` bakes the absolute path of
    ``ATTACHMENTS_DIR`` into ``attachment.filename`` — rebuilding it in place
    would rewrite a committed file with a machine-specific path.
    """
    scratch_db = tmp_path / "chat.db"
    monkeypatch.setattr(build_chat_db, "CHAT_DB_PATH", scratch_db)

    build(force=True)
    snapshot1 = tmp_path / "snap1.db"
    snapshot1.write_bytes(scratch_db.read_bytes())

    build(force=True)
    snapshot2 = tmp_path / "snap2.db"
    snapshot2.write_bytes(scratch_db.read_bytes())

    assert _sha256(snapshot1) == _sha256(snapshot2), (
        "two consecutive rebuilds produced different chat.db bytes"
    )


# ---------------------------------------------------------------------------
# Error handling: missing referenced attachment file
# ---------------------------------------------------------------------------


def test_build_fails_fast_when_attachment_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the referenced attachment file is gone from disk, the builder
    raises ``FileNotFoundError`` with a clear message — it does not produce
    a chat.db with a dangling reference."""
    fake_attachments_dir = tmp_path / "attachments"
    fake_attachments_dir.mkdir()
    fake_db_path = tmp_path / "chat.db"

    monkeypatch.setattr(build_chat_db, "ATTACHMENTS_DIR", fake_attachments_dir)
    monkeypatch.setattr(build_chat_db, "CHAT_DB_PATH", fake_db_path)

    # Create the attachment so _ensure_attachment_file passes, then delete
    # it before validation runs.
    (fake_attachments_dir / ATTACHMENT_FILENAME).write_bytes(b"x")
    (fake_attachments_dir / ATTACHMENT_FILENAME).unlink()

    # Patch the helper that auto-creates the file so the missing-file path
    # is exercised cleanly.
    monkeypatch.setattr(build_chat_db, "_ensure_attachment_file", lambda _p: None)

    with pytest.raises(FileNotFoundError, match="missing"):
        build_chat_db.build(force=True)


# ---------------------------------------------------------------------------
# Unit / integration counts (per spec testing requirements)
# ---------------------------------------------------------------------------


def test_row_counts_match_seed(ro_conn: sqlite3.Connection) -> None:
    """Builder produces a DB whose row counts match the seed plan."""
    counts = {
        "handle": ro_conn.execute("SELECT count(*) FROM handle").fetchone()[0],
        "chat": ro_conn.execute("SELECT count(*) FROM chat").fetchone()[0],
        "chat_handle_join": ro_conn.execute("SELECT count(*) FROM chat_handle_join").fetchone()[0],
        "message": ro_conn.execute("SELECT count(*) FROM message").fetchone()[0],
        "chat_message_join": ro_conn.execute("SELECT count(*) FROM chat_message_join").fetchone()[
            0
        ],
        "attachment": ro_conn.execute("SELECT count(*) FROM attachment").fetchone()[0],
        "message_attachment_join": ro_conn.execute(
            "SELECT count(*) FROM message_attachment_join"
        ).fetchone()[0],
    }
    assert counts == {
        "handle": 2,
        "chat": 2,
        "chat_handle_join": 2,
        "message": 6,
        "chat_message_join": 6,
        "attachment": 1,
        "message_attachment_join": 1,
    }


def test_attachments_directory_committed() -> None:
    """The referenced attachment file is present on disk under the
    fixtures/attachments/ directory."""
    assert (ATTACHMENTS_DIR / ATTACHMENT_FILENAME).exists()
