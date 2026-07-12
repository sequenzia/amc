"""Round-trip tests for the ``001_init`` Alembic migration (spec §9.1).

These tests complement :mod:`tests.core.test_schema`, which exhaustively
asserts the post-migration schema shape. This module focuses on the
*operational* properties the §9.1 Checkpoint Gate calls out:

- Migration applies cleanly on a fresh DB.
- Re-running the migration on an existing (already-stamped) DB is a no-op.
- A full ``upgrade head`` → ``downgrade base`` → ``upgrade head`` cycle
  succeeds without errors or stale objects.

We drive Alembic through its programmatic API
(:func:`alembic.command.upgrade` / :func:`alembic.command.downgrade`) and
inject the per-test SQLite URL via ``Config(cmd_opts=Namespace(x=[...]))``,
which mirrors the CLI ``alembic -x url=sqlite:///... upgrade head`` form
that ``amg/migrations/env.py`` already supports. This avoids leaning on the
``$AMG_DB_PATH`` env var so the tests are independent of the surrounding
process environment.
"""

from __future__ import annotations

import sqlite3
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# The current head revision; bump as new migrations land. Each new revision
# should append its own assertions if the upgrade introduces visible side
# effects beyond the alembic_version stamp.
HEAD_REVISION = "002_outbound_status"

# Indexes named in spec §7.3.3. Every one of these must exist after
# ``upgrade head`` and must be gone after ``downgrade base``.
EXPECTED_INDEXES = (
    "idx_messages_channel_ts",
    "idx_messages_allowlist_ts",
    "idx_messages_source_created",
    "idx_reads_agent",
    "idx_webhook_pending",
    "idx_idem_expires",
    "idx_senders_person",
    "idx_attachments_message",
    "idx_attachments_created",
)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(db_path: Path) -> Config:
    """Build an Alembic :class:`Config` pinned to ``db_path``.

    Uses ``cmd_opts.x = ["url=sqlite:///<path>"]`` which our ``env.py``
    consumes via :meth:`EnvironmentContext.get_x_argument`. This is the
    programmatic equivalent of ``alembic -x url=... upgrade head``.
    """
    cfg = Config(str(ALEMBIC_INI))
    cfg.cmd_opts = Namespace(x=[f"url=sqlite:///{db_path}"])
    return cfg


def _list_tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _list_user_indexes(db_path: Path) -> set[str]:
    """Return every named index, excluding SQLite's autoindexes for PKs/UNIQUEs."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _alembic_version(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Per-test SQLite file path (the file itself is created by Alembic)."""
    return tmp_path / "test.db"


@pytest.fixture
def upgraded_db(db_path: Path) -> Iterator[Path]:
    """Apply ``upgrade head`` once, yielding the resulting DB path."""
    cfg = _make_config(db_path)
    command.upgrade(cfg, "head")
    yield db_path


# ---------------------------------------------------------------------------
# Fresh DB
# ---------------------------------------------------------------------------


def test_fresh_upgrade_creates_all_tables(upgraded_db: Path) -> None:
    """Acceptance: fresh-DB upgrade produces every §7.3 table."""
    tables = _list_tables(upgraded_db)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"Missing tables after fresh upgrade: {sorted(missing)}"


def test_fresh_upgrade_stamps_revision(upgraded_db: Path) -> None:
    assert _alembic_version(upgraded_db) == [HEAD_REVISION]


@pytest.mark.parametrize("index_name", EXPECTED_INDEXES)
def test_fresh_upgrade_creates_expected_index(upgraded_db: Path, index_name: str) -> None:
    """Acceptance: every index named in §7.3.3 is present after upgrade."""
    indexes = _list_user_indexes(upgraded_db)
    assert index_name in indexes, f"Index {index_name!r} missing. Got: {sorted(indexes)}"


# ---------------------------------------------------------------------------
# Existing DB (idempotency)
# ---------------------------------------------------------------------------


def test_upgrade_on_existing_db_is_noop(db_path: Path) -> None:
    """Acceptance: applying ``upgrade head`` twice succeeds and stays at head.

    This proves the migration is safe to re-run against an already-stamped
    DB — no duplicate-create errors, no version bump.
    """
    cfg = _make_config(db_path)
    command.upgrade(cfg, "head")
    tables_after_first = _list_tables(db_path)
    indexes_after_first = _list_user_indexes(db_path)

    # Second invocation: must be a no-op.
    command.upgrade(cfg, "head")

    assert _alembic_version(db_path) == [HEAD_REVISION]
    assert _list_tables(db_path) == tables_after_first
    assert _list_user_indexes(db_path) == indexes_after_first


# ---------------------------------------------------------------------------
# Round-trip: upgrade -> downgrade -> upgrade
# ---------------------------------------------------------------------------


def test_round_trip_upgrade_downgrade_upgrade(db_path: Path) -> None:
    """Acceptance: full round-trip leaves the schema in its post-upgrade shape.

    ``upgrade head`` → ``downgrade base`` → ``upgrade head`` must succeed
    end-to-end. After the final upgrade the DB must look identical to a
    single fresh upgrade: every expected table, every expected index, and
    the revision stamped at ``001_init``.
    """
    cfg = _make_config(db_path)

    # 1. Initial upgrade.
    command.upgrade(cfg, "head")
    tables_initial = _list_tables(db_path)
    indexes_initial = _list_user_indexes(db_path)
    assert _alembic_version(db_path) == [HEAD_REVISION]

    # 2. Full downgrade. Only ``alembic_version`` should remain
    # (Alembic does not drop its own bookkeeping table).
    command.downgrade(cfg, "base")
    remaining = _list_tables(db_path) - {"alembic_version"}
    assert remaining == set(), f"downgrade base left stale tables: {sorted(remaining)}"
    assert _list_user_indexes(db_path) == set(), "downgrade base left stale indexes"
    assert _alembic_version(db_path) == [], "downgrade base did not clear the alembic_version row"

    # 3. Re-upgrade. Schema must come back identical to the first upgrade.
    command.upgrade(cfg, "head")
    assert _list_tables(db_path) == tables_initial
    assert _list_user_indexes(db_path) == indexes_initial
    assert _alembic_version(db_path) == [HEAD_REVISION]
