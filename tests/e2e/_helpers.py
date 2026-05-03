"""Shared helpers for end-to-end tests in :mod:`tests.e2e`.

These helpers exist so individual e2e test modules don't have to copy
the same wiring boilerplate. They intentionally have no project-wide
imports beyond the ones already used by `tests.connectors` /
`tests.fakes` so they remain cheap to import.

Functions exposed:

* :func:`apply_alembic_head` -- run ``alembic upgrade head`` against an
  arbitrary SQLite path.
* :func:`build_inmemory_allowlist` -- construct an :class:`AllowlistLoader`
  from explicit ``(source, id, display_name, person_id)`` tuples without
  parsing TOML. Lifted from the pattern used by every connector test.
* :func:`make_dm_payload` -- build a minimal Discord ``MESSAGE_CREATE``
  payload that the :class:`tests.fakes.discord_gateway.FakeDiscordGateway`
  can hand to ``discord.py``.
* :func:`append_chat_db_inbound_row` -- append a single inbound row
  to a writable chat.db copy. Mirrors the helper in
  ``tests/connectors/imessage/test_connector.py`` so we don't
  cross-import test modules (see "Helper imports across test modules"
  in execution_context.md).
* :func:`read_messages_count`, :func:`read_imessage_cursor`,
  :func:`read_discord_cursor`, :func:`list_messages_for_assertion`,
  :func:`list_webhook_deliveries` -- read-only DB introspection.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from amc.core.allowlist import AllowlistEntry, AllowlistLoader
from amc.core.envelope import Source

__all__ = [
    "append_chat_db_inbound_row",
    "apply_alembic_head",
    "build_inmemory_allowlist",
    "list_messages_for_assertion",
    "list_webhook_deliveries",
    "make_dm_payload",
    "read_discord_cursor",
    "read_imessage_cursor",
    "read_messages_count",
]


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def apply_alembic_head(alembic_ini: Path, db_path: Path) -> None:
    """Run ``alembic upgrade head`` against ``db_path``.

    ``alembic.command.upgrade`` reads ``AMC_DB_PATH`` from the
    environment via :func:`amc.core.db.create_engine_from_env`; the
    caller is responsible for setting that env var with
    ``monkeypatch.setenv`` before calling this helper.
    """
    cfg = Config(str(alembic_ini))
    command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Allowlist (in-memory)
# ---------------------------------------------------------------------------


def build_inmemory_allowlist(
    entries: Iterable[tuple[Source, str, str, str | None]],
) -> AllowlistLoader:
    """Build an :class:`AllowlistLoader` from explicit tuples.

    Each tuple is ``(source, id, display_name, person_id)``. The
    constructor of :class:`AllowlistLoader` parses TOML; we bypass it
    via ``__new__`` to keep the test self-contained.
    """
    loader = AllowlistLoader.__new__(AllowlistLoader)
    loader.path = None  # type: ignore[assignment]
    loader._entries = {
        (src, ident): AllowlistEntry(
            source=src,
            id=ident,
            display_name=display_name,
            person_id=person_id,
        )
        for (src, ident, display_name, person_id) in entries
    }
    return loader


# ---------------------------------------------------------------------------
# Discord MESSAGE_CREATE payload
# ---------------------------------------------------------------------------


def make_dm_payload(
    *,
    message_id: str,
    channel_id: str,
    author_id: str,
    content: str,
    timestamp: str = "2026-05-03T12:00:00.000000+00:00",
    username: str = "alice",
    global_name: str = "Alice",
) -> dict[str, Any]:
    """Build a Discord MESSAGE_CREATE payload (no ``guild_id`` -> DM)."""
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author": {
            "id": author_id,
            "username": username,
            "discriminator": "0",
            "global_name": global_name,
            "avatar": None,
            "bot": False,
        },
        "content": content,
        "timestamp": timestamp,
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


# ---------------------------------------------------------------------------
# chat.db row appender
# ---------------------------------------------------------------------------


def append_chat_db_inbound_row(
    chat_db: Path,
    *,
    rowid: int,
    text_value: str,
    handle_id: int = 1,
    is_from_me: int = 0,
    chat_id: int = 1,
) -> None:
    """Append one inbound row to a writable chat.db copy.

    Mirrors the columns the Phase 0 fixture builder writes for plain
    text messages (``handle_id=1`` is the allowlisted sender; ``chat_id=1``
    is the DM thread).
    """
    conn = sqlite3.connect(chat_db)
    try:
        conn.execute("BEGIN")
        date_ns = 1_000_000_000_000_000_000 + rowid * 1_000_000_000
        guid = f"e2e-relaunch-{rowid:020d}"
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
            "INSERT INTO chat_message_join (chat_id, message_id, message_date) VALUES (?, ?, ?)",
            (chat_id, rowid, date_ns),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read-only DB introspection
# ---------------------------------------------------------------------------


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def read_messages_count(db_path: Path, *, source: str | None = None) -> int:
    """Return the number of rows in ``messages`` (optionally filtered by source)."""
    conn = _connect_ro(db_path)
    try:
        if source is None:
            row = conn.execute("SELECT count(*) FROM messages").fetchone()
        else:
            row = conn.execute(
                "SELECT count(*) FROM messages WHERE source = ?", (source,)
            ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def read_imessage_cursor(db_path: Path) -> str | None:
    """Return ``connector_state.cursor`` for ``source='imessage'`` (or ``None``)."""
    return _read_connector_cursor(db_path, "imessage")


def read_discord_cursor(db_path: Path) -> str | None:
    """Return ``connector_state.cursor`` for ``source='discord'`` (or ``None``)."""
    return _read_connector_cursor(db_path, "discord")


def _read_connector_cursor(db_path: Path, source: str) -> str | None:
    conn = _connect_ro(db_path)
    try:
        row = conn.execute(
            "SELECT cursor FROM connector_state WHERE source = ?", (source,)
        ).fetchone()
    finally:
        conn.close()
    return row["cursor"] if row is not None else None


def list_messages_for_assertion(db_path: Path) -> list[dict[str, Any]]:
    """Return every messages row as a list of dicts (id, source, direction, text)."""
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT id, source, direction, text, channel_id "
            "FROM messages ORDER BY message_ts ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_webhook_deliveries(db_path: Path) -> list[dict[str, Any]]:
    """Return every webhook_deliveries row, oldest first."""
    conn = _connect_ro(db_path)
    try:
        with contextlib.suppress(sqlite3.OperationalError):
            rows = conn.execute(
                "SELECT * FROM webhook_deliveries ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]
        return []
    finally:
        conn.close()
