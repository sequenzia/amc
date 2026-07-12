"""Tests for ``amg.core.schema`` + the ``001_init`` Alembic migration.

These tests apply the migration to a tmp_path SQLite DB and introspect
``sqlite_master`` to assert every index, table, and ``CHECK`` constraint
required by spec §7.3.3 is present. We use Alembic's programmatic API so the
test suite has no shell/CLI dependency.

Coverage:

- All nine tables exist after ``upgrade head``
- Every required index (named per spec) exists
- Every CHECK constraint mentioned in §7.3.3 fires (probed via failing INSERT)
- WAL mode is enabled by :func:`amg.core.db.create_engine_from_env`
- ``downgrade base`` removes everything except ``alembic_version``
- Re-running ``upgrade head`` is a no-op (idempotent)
- ``messages.allowlist_status = 'outbound'`` is permitted (spec note)
- FK violations are caught when ``foreign_keys = ON``
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from amg.core.db import build_database_url, create_engine_from_env

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Bump as new migrations land. Tests that pin to a specific revision should
# update both the constant and (if necessary) any new schema assertions.
HEAD_REVISION = "002_outbound_status"

EXPECTED_TABLES = {
    "messages",
    "channels",
    "senders",
    "identity_links",
    "message_reads",
    "attachments",
    "webhook_deliveries",
    "idempotency_keys",
    "connector_state",
}

EXPECTED_INDEXES = {
    "idx_messages_channel_ts",
    "idx_messages_allowlist_ts",
    "idx_messages_source_created",
    "idx_senders_person",
    "idx_reads_agent",
    "idx_attachments_message",
    "idx_attachments_created",
    "idx_webhook_pending",
    "idx_idem_expires",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file.

    Yields the path. The Alembic env.py reads ``$AMG_DB_PATH``; we set it
    here so the migration targets our throwaway DB rather than the dev
    default location.
    """
    db_path = tmp_path / "schema.db"
    monkeypatch.setenv("AMG_DB_PATH", str(db_path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    yield db_path


def _connect(db_path: Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# ---------------------------------------------------------------------------
# Functional: tables, indexes
# ---------------------------------------------------------------------------


def test_upgrade_head_creates_all_tables(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r[0] for r in rows}
    missing = EXPECTED_TABLES - names
    assert not missing, f"Missing tables: {missing}. Got: {sorted(names)}"


def test_upgrade_head_creates_all_indexes(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
    ).fetchall()
    names = {r[0] for r in rows}
    missing = EXPECTED_INDEXES - names
    assert not missing, f"Missing indexes: {missing}. Got: {sorted(names)}"


def test_alembic_version_is_stamped(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    assert rows == [(HEAD_REVISION,)]


# ---------------------------------------------------------------------------
# Functional: CHECK constraints (every CHECK from §7.3.3 must fire)
# ---------------------------------------------------------------------------


def _seed_channel_and_sender(conn: sqlite3.Connection) -> None:
    """Insert a valid channel + sender so messages-row tests can build on them."""
    conn.execute(
        "INSERT INTO channels (source, channel_id, channel_type) "
        "VALUES ('imessage', '+15551234567', 'dm')"
    )
    conn.execute(
        "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
        "VALUES ('imessage', '+15551234567', 'Alice', 'allowed')"
    )
    conn.commit()


def test_messages_source_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    _seed_channel_and_sender(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts) "
            "VALUES ('msg_x', 'slack', '+15551234567', 'dm', '+15551234567', "
            "'hi', 'inbound', 'allowed', '2026-01-01T00:00:00Z')"
        )


def test_messages_channel_type_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    _seed_channel_and_sender(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts) "
            "VALUES ('msg_x', 'imessage', '+15551234567', 'broadcast', "
            "'+15551234567', 'hi', 'inbound', 'allowed', "
            "'2026-01-01T00:00:00Z')"
        )


def test_messages_direction_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    _seed_channel_and_sender(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts) "
            "VALUES ('msg_x', 'imessage', '+15551234567', 'dm', "
            "'+15551234567', 'hi', 'sideways', 'allowed', "
            "'2026-01-01T00:00:00Z')"
        )


def test_messages_allowlist_status_check_rejects_garbage(
    fresh_db: Path,
) -> None:
    conn = _connect(fresh_db)
    _seed_channel_and_sender(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts) "
            "VALUES ('msg_x', 'imessage', '+15551234567', 'dm', "
            "'+15551234567', 'hi', 'inbound', 'banned', "
            "'2026-01-01T00:00:00Z')"
        )


def test_messages_allowlist_status_accepts_outbound(fresh_db: Path) -> None:
    """Spec §7.3.3 note: ``'outbound'`` is reserved for agent-sent messages."""
    conn = _connect(fresh_db)
    _seed_channel_and_sender(conn)
    conn.execute(
        "INSERT INTO messages "
        "(id, source, channel_id, channel_type, sender_id, text, "
        " direction, allowlist_status, message_ts) "
        "VALUES ('msg_out', 'imessage', '+15551234567', 'dm', "
        "'+15551234567', 'reply', 'outbound', 'outbound', "
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    rows = conn.execute("SELECT allowlist_status FROM messages WHERE id='msg_out'").fetchall()
    assert rows == [("outbound",)]


def test_channels_source_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) "
            "VALUES ('telegram', 'c1', 'dm')"
        )


def test_channels_channel_type_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) "
            "VALUES ('imessage', 'c1', 'broadcast')"
        )


def test_senders_source_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES ('telegram', 's1', 'X', 'allowed')"
        )


def test_senders_allowlist_status_check_rejects_outbound(
    fresh_db: Path,
) -> None:
    """Spec §7.3.3 note: ``'outbound'`` does NOT apply to senders."""
    conn = _connect(fresh_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES ('imessage', 's1', 'X', 'outbound')"
        )


def test_identity_links_source_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO identity_links (person_id, source, sender_id) "
            "VALUES ('p1', 'telegram', 's1')"
        )


def test_webhook_status_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    _seed_channel_and_sender(conn)
    conn.execute(
        "INSERT INTO messages "
        "(id, source, channel_id, channel_type, sender_id, text, "
        " direction, allowlist_status, message_ts) "
        "VALUES ('msg_a', 'imessage', '+15551234567', 'dm', "
        "'+15551234567', 'hi', 'inbound', 'allowed', "
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO webhook_deliveries (id, message_id, status) "
            "VALUES ('wh_1', 'msg_a', 'queued')"
        )


def test_connector_state_source_check(fresh_db: Path) -> None:
    conn = _connect(fresh_db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO connector_state (source, cursor) VALUES ('email', '0')")


# ---------------------------------------------------------------------------
# Edge cases: idempotency + downgrade
# ---------------------------------------------------------------------------


def test_upgrade_head_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "idem.db"
    monkeypatch.setenv("AMG_DB_PATH", str(db_path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    # Second upgrade must be a no-op — no error, version unchanged.
    command.upgrade(cfg, "head")
    conn = _connect(db_path)
    rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    assert rows == [(HEAD_REVISION,)]


def test_downgrade_base_drops_everything_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "down.db"
    monkeypatch.setenv("AMG_DB_PATH", str(db_path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    conn = _connect(db_path)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    # Only alembic_version should remain (alembic doesn't drop its own table).
    assert tables - {"alembic_version"} == set()


# ---------------------------------------------------------------------------
# Error handling: FK enforcement
# ---------------------------------------------------------------------------


def test_messages_fk_to_channels_rejects_unknown_channel(
    fresh_db: Path,
) -> None:
    conn = _connect(fresh_db)
    # Sender exists, channel does not.
    conn.execute(
        "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
        "VALUES ('imessage', 's1', 'A', 'allowed')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts) "
            "VALUES ('msg_x', 'imessage', 'nope', 'dm', 's1', "
            "'hi', 'inbound', 'allowed', '2026-01-01T00:00:00Z')"
        )
        conn.commit()


def test_attachments_fk_to_messages_rejects_unknown_message(
    fresh_db: Path,
) -> None:
    conn = _connect(fresh_db)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO attachments "
            "(id, message_id, mime, size_bytes) "
            "VALUES ('att_1', 'msg_does_not_exist', 'image/png', 100)"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# WAL mode (applied at adapter startup, not in migration)
# ---------------------------------------------------------------------------


def test_create_engine_from_env_enables_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "wal.db"
    monkeypatch.setenv("AMG_DB_PATH", str(db_path))
    engine = create_engine_from_env()

    async def _check() -> str:
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("PRAGMA journal_mode;")
            row = result.fetchone()
            return row[0]
        # Note: dispose is awaited outside.

    mode = asyncio.run(_check())
    asyncio.run(engine.dispose())
    assert mode.lower() == "wal", f"Expected WAL, got {mode!r}"


def test_create_engine_from_env_enables_fk_enforcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "fk.db"
    monkeypatch.setenv("AMG_DB_PATH", str(db_path))
    engine = create_engine_from_env()

    async def _check() -> int:
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("PRAGMA foreign_keys;")
            row = result.fetchone()
            return row[0]

    enabled = asyncio.run(_check())
    asyncio.run(engine.dispose())
    assert enabled == 1


def test_build_database_url_from_path(tmp_path: Path) -> None:
    p = tmp_path / "x.db"
    assert build_database_url(p) == f"sqlite+aiosqlite:///{p}"


def test_build_database_url_in_memory() -> None:
    assert build_database_url(":memory:") == "sqlite+aiosqlite:///:memory:"
