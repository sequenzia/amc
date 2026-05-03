"""Tests for ``amc.core.sweepers`` (spec §10.5 idempotency + attachment sweepers).

Coverage:

* :func:`sweep_idempotency_keys` (one-shot helper):
    - empty table → 0
    - all-fresh table → 0 (live rows preserved)
    - mixed table → expired rows deleted, live rows preserved
    - idempotent: re-running deletes nothing
    - boundary (``expires_at == now``): row preserved (strict ``<``)
    - injectable clock fast-forwards past ``expires_at`` → deletion

* :class:`IdempotencySweeper`:
    - ``tick()`` delegates to the helper and emits the structured log
    - ``start()`` is re-entrant (does not spawn a second task)
    - ``stop()`` cancels the loop; subsequent ``stop()`` is a no-op
    - loop drives at least one tick within the interval

* :func:`sweep_attachments` (one-shot helper):
    - empty table / all-fresh / boundary handling
    - mixed table → old rows have file unlinked + ``bytes_path`` nulled,
      fresh rows untouched
    - rows with ``bytes_path IS NULL`` skipped (already swept)
    - idempotent: re-running deletes nothing
    - missing file on disk → log WARN, row still nulled, counted in deleted
    - permission denied on unlink → log ERROR, row preserved, counted in skipped
    - single-row failure does NOT abort the rest of the sweep

* :class:`AttachmentSweeper`:
    - ``tick()`` returns ``(deleted, skipped)`` and emits the structured log
    - retention_days resolves from arg → env → default 90
    - ``start()`` re-entrant; ``stop()`` cancels and is idempotent
    - loop drives at least one tick within a short interval

The DB is a tmp_path SQLite file with the full schema applied via
``alembic upgrade head`` — same pattern used in ``test_idempotency.py`` and
``test_webhook.py``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.sweepers import (
    DEFAULT_ATTACHMENT_RETENTION_DAYS,
    DEFAULT_ATTACHMENT_SWEEP_INTERVAL_SECONDS,
    DEFAULT_SWEEP_INTERVAL_SECONDS,
    ENV_ATTACHMENT_RETENTION_DAYS,
    AttachmentSweeper,
    IdempotencySweeper,
    sweep_attachments,
    sweep_idempotency_keys,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Same canonical ISO format the production code writes (millisecond precision,
# trailing 'Z'). Picked to match ``amc.core.idempotency._format_ts`` so seeded
# rows sort identically to live writes.
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


# ---------------------------------------------------------------------------
# Clock fake
# ---------------------------------------------------------------------------


class _FakeClock:
    """Manually-advanced UTC clock for deterministic sweep tests."""

    __slots__ = ("now",)

    def __init__(self, start: datetime | None = None) -> None:
        self.now: datetime = start or datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""

    path = tmp_path / "sweeper.db"
    monkeypatch.setenv("AMC_DB_PATH", str(path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    return path


@pytest.fixture
def session_factory(db_path: Path) -> Iterator[async_sessionmaker[AsyncSession]]:
    """Async session factory bound to the migrated tmp_path DB.

    Engine disposal runs in a fresh ``asyncio.run`` because pytest-asyncio's
    loop may already be closed at fixture teardown — same workaround used in
    ``test_webhook.py``.
    """

    engine = create_engine_from_env(db_path_override=db_path)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        import contextlib as _ctx

        with _ctx.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------


async def _insert_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    key: str,
    expires_at: datetime,
    request_hash: str = "h",
    response_status: int = 200,
    response_body: str = "{}",
) -> None:
    """Insert one row into ``idempotency_keys`` with the given ``expires_at``."""

    aware = expires_at.astimezone(UTC) if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    expires_iso = aware.strftime(_ISO_FORMAT)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO idempotency_keys "
                "(key, request_hash, response_status, response_body, expires_at) "
                "VALUES (:key, :hash, :status, :body, :expires_at)"
            ),
            {
                "key": key,
                "hash": request_hash,
                "status": response_status,
                "body": response_body,
                "expires_at": expires_iso,
            },
        )
        await session.commit()


async def _count_keys(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM idempotency_keys"))
        return int(result.scalar_one())


async def _fetch_key(
    session_factory: async_sessionmaker[AsyncSession],
    key: str,
) -> str | None:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT key FROM idempotency_keys WHERE key = :k"),
            {"k": key},
        )
        row = result.first()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# sweep_idempotency_keys (one-shot)
# ---------------------------------------------------------------------------


class TestSweepIdempotencyKeys:
    async def test_empty_table_is_noop(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            deleted = await sweep_idempotency_keys(session)
        assert deleted == 0

    async def test_all_fresh_rows_preserved(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        # Two rows that expire well in the future.
        await _insert_key(
            session_factory,
            key="fresh-1",
            expires_at=clock.now + timedelta(hours=10),
        )
        await _insert_key(
            session_factory,
            key="fresh-2",
            expires_at=clock.now + timedelta(hours=20),
        )

        async with session_factory() as session:
            deleted = await sweep_idempotency_keys(session, now=clock.now)
        assert deleted == 0
        assert await _count_keys(session_factory) == 2

    async def test_expired_rows_deleted_live_rows_preserved(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        # Two expired (10 min ago, 1 min ago) and two live (10 min away, 1h away).
        await _insert_key(
            session_factory,
            key="expired-old",
            expires_at=clock.now - timedelta(minutes=10),
        )
        await _insert_key(
            session_factory,
            key="expired-recent",
            expires_at=clock.now - timedelta(minutes=1),
        )
        await _insert_key(
            session_factory,
            key="live-soon",
            expires_at=clock.now + timedelta(minutes=10),
        )
        await _insert_key(
            session_factory,
            key="live-far",
            expires_at=clock.now + timedelta(hours=1),
        )

        async with session_factory() as session:
            deleted = await sweep_idempotency_keys(session, now=clock.now)
        assert deleted == 2

        # Live rows still present, expired ones gone.
        assert await _fetch_key(session_factory, "expired-old") is None
        assert await _fetch_key(session_factory, "expired-recent") is None
        assert await _fetch_key(session_factory, "live-soon") == "live-soon"
        assert await _fetch_key(session_factory, "live-far") == "live-far"

    async def test_idempotent_second_sweep_is_noop(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        await _insert_key(
            session_factory,
            key="expired",
            expires_at=clock.now - timedelta(seconds=1),
        )
        await _insert_key(
            session_factory,
            key="live",
            expires_at=clock.now + timedelta(hours=1),
        )

        async with session_factory() as session:
            first = await sweep_idempotency_keys(session, now=clock.now)
        assert first == 1

        # Second sweep at the same instant: nothing left to delete.
        async with session_factory() as session:
            second = await sweep_idempotency_keys(session, now=clock.now)
        assert second == 0
        assert await _count_keys(session_factory) == 1

    async def test_boundary_row_at_now_is_preserved(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        """``expires_at == now`` should NOT be swept (strict ``<``)."""

        await _insert_key(session_factory, key="boundary", expires_at=clock.now)
        async with session_factory() as session:
            deleted = await sweep_idempotency_keys(session, now=clock.now)
        assert deleted == 0
        assert await _fetch_key(session_factory, "boundary") == "boundary"

    async def test_clock_fast_forward_triggers_deletion(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        """At t0 a row is fresh; at t0+25h the same row is swept."""

        # Row expires 24h after the start instant — exactly the production TTL.
        expires = clock.now + timedelta(hours=24)
        await _insert_key(session_factory, key="ttl-24h", expires_at=expires)

        # Just before TTL → still fresh.
        async with session_factory() as session:
            deleted = await sweep_idempotency_keys(
                session,
                now=clock.now + timedelta(hours=23, minutes=59),
            )
        assert deleted == 0
        assert await _fetch_key(session_factory, "ttl-24h") == "ttl-24h"

        # Fast-forward past expires_at via the injectable clock.
        clock.advance(timedelta(hours=25))
        async with session_factory() as session:
            deleted = await sweep_idempotency_keys(session, now=clock.now)
        assert deleted == 1
        assert await _fetch_key(session_factory, "ttl-24h") is None

    async def test_default_now_uses_wall_clock(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Omitting ``now`` falls back to ``datetime.now(tz=UTC)``."""

        # Row expired well in the past — the wall-clock default must catch it.
        await _insert_key(
            session_factory,
            key="ancient",
            expires_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        async with session_factory() as session:
            deleted = await sweep_idempotency_keys(session)
        assert deleted == 1


# ---------------------------------------------------------------------------
# IdempotencySweeper class
# ---------------------------------------------------------------------------


class TestIdempotencySweeper:
    async def test_tick_delegates_to_helper(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        await _insert_key(
            session_factory,
            key="expired",
            expires_at=clock.now - timedelta(seconds=1),
        )
        await _insert_key(
            session_factory,
            key="live",
            expires_at=clock.now + timedelta(hours=1),
        )

        sweeper = IdempotencySweeper(
            session_factory=session_factory,
            time_provider=clock,
        )
        deleted = await sweeper.tick()
        assert deleted == 1
        assert await _count_keys(session_factory) == 1
        assert await _fetch_key(session_factory, "live") == "live"

    async def test_tick_logs_idempotency_sweep_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Per spec: ``event=idempotency_sweep deleted={n}``."""

        await _insert_key(
            session_factory,
            key="expired-1",
            expires_at=clock.now - timedelta(seconds=1),
        )
        await _insert_key(
            session_factory,
            key="expired-2",
            expires_at=clock.now - timedelta(seconds=2),
        )

        sweeper = IdempotencySweeper(
            session_factory=session_factory,
            time_provider=clock,
        )

        # Bridge structlog into stdlib so ``caplog`` sees the records.
        structlog.configure(
            processors=[structlog.stdlib.render_to_log_kwargs],
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
        try:
            with caplog.at_level(logging.INFO, logger="amc.sweepers"):
                deleted = await sweeper.tick()
        finally:
            structlog.reset_defaults()

        assert deleted == 2
        # The structured log call lands as ``event=idempotency_sweep`` with
        # ``deleted=2`` extra. We assert on either the message or the
        # extra-args payload depending on how stdlib formatted it.
        sweep_records = [r for r in caplog.records if "idempotency_sweep" in r.getMessage()]
        all_msgs = [r.getMessage() for r in caplog.records]
        assert sweep_records, f"no idempotency_sweep log; got {all_msgs}"
        # ``deleted=2`` is rendered into either the message or as a record attr.
        record = sweep_records[0]
        assert getattr(record, "deleted", None) == 2 or "deleted=2" in record.getMessage()

    async def test_start_is_reentrant(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        """Calling start() twice must not spawn a second background task."""

        sweeper = IdempotencySweeper(
            session_factory=session_factory,
            time_provider=clock,
            sweep_interval_seconds=60.0,
        )
        await sweeper.start()
        first_task = sweeper._task
        await sweeper.start()
        second_task = sweeper._task
        try:
            assert first_task is second_task
        finally:
            await sweeper.stop()

    async def test_stop_cancels_loop_and_is_idempotent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        sweeper = IdempotencySweeper(
            session_factory=session_factory,
            time_provider=clock,
            sweep_interval_seconds=60.0,
        )
        await sweeper.start()
        await sweeper.stop()
        # Second stop is a no-op.
        await sweeper.stop()
        assert sweeper._task is None

    async def test_loop_drives_a_tick_within_short_interval(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        """With a sub-second interval the loop runs at least one sweep."""

        await _insert_key(
            session_factory,
            key="expired",
            expires_at=clock.now - timedelta(seconds=1),
        )

        sweeper = IdempotencySweeper(
            session_factory=session_factory,
            time_provider=clock,
            sweep_interval_seconds=0.01,
        )
        await sweeper.start()
        # Yield enough times for the background task to run its first tick
        # and re-enter the wait. 50ms is overkill at a 10ms interval but
        # cheap on CI.
        await asyncio.sleep(0.05)
        await sweeper.stop()

        # The expired row was deleted by the loop (no manual tick() call).
        assert await _fetch_key(session_factory, "expired") is None

    async def test_default_sweep_interval_matches_spec(self) -> None:
        """Spec §10.5 mandates an hourly cadence (3600s)."""

        assert DEFAULT_SWEEP_INTERVAL_SECONDS == 3600.0

    async def test_default_time_provider_is_utcnow(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Omitting ``time_provider`` defaults to wall-clock UTC."""

        await _insert_key(
            session_factory,
            key="ancient",
            expires_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        sweeper = IdempotencySweeper(session_factory=session_factory)
        deleted = await sweeper.tick()
        assert deleted == 1


# ---------------------------------------------------------------------------
# Attachment sweeper helpers
# ---------------------------------------------------------------------------


# A minimal FK chain (channel → sender → message) is required before we can
# insert into ``attachments`` because the schema enforces FKs (spec §7.3.3).
_ATTACH_CHANNEL_ID = "imessage:test-channel"
_ATTACH_SENDER_ID = "+15551234567"
_ATTACH_MESSAGE_ID = "msg_01HABCDEFGHJKMNPQRSTVWXYZ0"


async def _seed_message_chain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Insert the channel/sender/message rows needed by attachment FKs."""

    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO channels (source, channel_id, channel_type) "
                "VALUES ('imessage', :cid, 'dm')"
            ),
            {"cid": _ATTACH_CHANNEL_ID},
        )
        await session.execute(
            text(
                "INSERT INTO senders "
                "(source, sender_id, display_name, allowlist_status, "
                " first_seen, last_seen) "
                "VALUES ('imessage', :sid, 'Test', 'allowed', :ts, :ts)"
            ),
            {
                "sid": _ATTACH_SENDER_ID,
                "ts": "2026-01-01T00:00:00.000Z",
            },
        )
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, source, channel_id, channel_type, sender_id, text, "
                " direction, allowlist_status, message_ts) "
                "VALUES (:id, 'imessage', :cid, 'dm', :sid, 'hi', "
                "        'inbound', 'allowed', :ts)"
            ),
            {
                "id": _ATTACH_MESSAGE_ID,
                "cid": _ATTACH_CHANNEL_ID,
                "sid": _ATTACH_SENDER_ID,
                "ts": "2026-01-01T00:00:00.000Z",
            },
        )
        await session.commit()


async def _insert_attachment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    attachment_id: str,
    created_at: datetime,
    bytes_path: str | None,
    mime: str = "image/png",
    size_bytes: int = 100,
    original_url_or_path: str = "/tmp/source.png",
) -> None:
    """Insert one row into ``attachments``.

    ``bytes_path`` may be ``None`` to simulate a row whose bytes were
    already swept by a prior run.
    """

    aware = created_at.astimezone(UTC) if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    created_iso = aware.strftime(_ISO_FORMAT)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO attachments "
                "(id, message_id, mime, size_bytes, bytes_path, "
                " original_url_or_path, created_at) "
                "VALUES (:id, :msg, :mime, :size, :bp, :orig, :created)"
            ),
            {
                "id": attachment_id,
                "msg": _ATTACH_MESSAGE_ID,
                "mime": mime,
                "size": size_bytes,
                "bp": bytes_path,
                "orig": original_url_or_path,
                "created": created_iso,
            },
        )
        await session.commit()


async def _fetch_attachment(
    session_factory: async_sessionmaker[AsyncSession],
    attachment_id: str,
) -> dict[str, object] | None:
    """Return the attachment row keyed by id, or ``None`` if absent."""

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT id, mime, size_bytes, bytes_path, "
                "       original_url_or_path, created_at "
                "FROM attachments WHERE id = :id"
            ),
            {"id": attachment_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    """Per-test directory simulating the attachment-store filesystem root."""

    d = tmp_path / "attachments"
    d.mkdir()
    return d


def _write_bytes(store_dir: Path, attachment_id: str, payload: bytes = b"png-bytes") -> Path:
    """Write a tiny on-disk file under ``store_dir`` named after ``attachment_id``."""

    path = store_dir / attachment_id
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# sweep_attachments (one-shot)
# ---------------------------------------------------------------------------


class TestSweepAttachments:
    async def test_empty_table_is_noop(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                retention_days=90,
            )
        assert (deleted, skipped) == (0, 0)

    async def test_all_fresh_rows_preserved(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        await _seed_message_chain(session_factory)

        # Two rows whose age is well within the retention window.
        for i in range(2):
            att_id = f"att_FRESH{i}________________________"[:30]
            file_path = _write_bytes(store_dir, att_id)
            await _insert_attachment(
                session_factory,
                attachment_id=att_id,
                created_at=clock.now - timedelta(days=10),
                bytes_path=str(file_path),
            )

        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                retention_days=90,
                now=clock.now,
            )
        assert (deleted, skipped) == (0, 0)

        # All files still on disk; bytes_path still set.
        for path in store_dir.iterdir():
            assert path.is_file()

    async def test_old_rows_swept_fresh_preserved(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        await _seed_message_chain(session_factory)

        old_id = "att_OLD0000000000000000000000"
        fresh_id = "att_FRESH00000000000000000000"
        old_path = _write_bytes(store_dir, old_id, b"old-payload")
        fresh_path = _write_bytes(store_dir, fresh_id, b"fresh-payload")

        await _insert_attachment(
            session_factory,
            attachment_id=old_id,
            created_at=clock.now - timedelta(days=100),
            bytes_path=str(old_path),
            size_bytes=len(b"old-payload"),
        )
        await _insert_attachment(
            session_factory,
            attachment_id=fresh_id,
            created_at=clock.now - timedelta(days=10),
            bytes_path=str(fresh_path),
            size_bytes=len(b"fresh-payload"),
        )

        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                retention_days=90,
                now=clock.now,
            )
        assert (deleted, skipped) == (1, 0)

        # Old file gone from disk; fresh file still there.
        assert not old_path.exists()
        assert fresh_path.exists()

        # Old row updated to bytes_path=NULL; other cols preserved.
        old_row = await _fetch_attachment(session_factory, old_id)
        assert old_row is not None
        assert old_row["bytes_path"] is None
        assert old_row["mime"] == "image/png"
        assert old_row["size_bytes"] == len(b"old-payload")
        assert old_row["original_url_or_path"] == "/tmp/source.png"

        # Fresh row untouched.
        fresh_row = await _fetch_attachment(session_factory, fresh_id)
        assert fresh_row is not None
        assert fresh_row["bytes_path"] == str(fresh_path)

    async def test_already_null_bytes_path_skipped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        """Rows with ``bytes_path IS NULL`` are not touched (already swept)."""

        await _seed_message_chain(session_factory)
        att_id = "att_PRESWEPT00000000000000000"
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now - timedelta(days=200),
            bytes_path=None,  # already swept by some prior run
        )

        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                retention_days=90,
                now=clock.now,
            )
        assert (deleted, skipped) == (0, 0)

        row = await _fetch_attachment(session_factory, att_id)
        assert row is not None
        assert row["bytes_path"] is None

    async def test_idempotent_second_sweep_is_noop(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        await _seed_message_chain(session_factory)

        att_id = "att_OLDTHENNULL00000000000000"
        file_path = _write_bytes(store_dir, att_id)
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now - timedelta(days=100),
            bytes_path=str(file_path),
        )

        async with session_factory() as session:
            first = await sweep_attachments(session, retention_days=90, now=clock.now)
        assert first == (1, 0)

        async with session_factory() as session:
            second = await sweep_attachments(session, retention_days=90, now=clock.now)
        assert second == (0, 0)

        row = await _fetch_attachment(session_factory, att_id)
        assert row is not None
        assert row["bytes_path"] is None

    async def test_boundary_row_at_threshold_is_preserved(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        """``created_at == now - retention_days`` should NOT be swept (strict ``<``)."""

        await _seed_message_chain(session_factory)
        att_id = "att_BOUNDARY00000000000000000"
        file_path = _write_bytes(store_dir, att_id)
        # Created EXACTLY at the threshold instant — should survive.
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now - timedelta(days=90),
            bytes_path=str(file_path),
        )

        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                retention_days=90,
                now=clock.now,
            )
        assert (deleted, skipped) == (0, 0)
        assert file_path.exists()

    async def test_missing_file_on_disk_logs_warn_and_nulls_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        """Bytes already gone from disk → WARN, row still nulled, counted in deleted."""

        await _seed_message_chain(session_factory)
        att_id = "att_GHOST000000000000000000000"
        ghost_path = store_dir / att_id  # never created
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now - timedelta(days=100),
            bytes_path=str(ghost_path),
        )

        # Spy on the module-level structlog logger directly so we don't
        # depend on test-order-sensitive structlog-to-stdlib bridging
        # (the lazy proxy binds at first use; resetting defaults does not
        # rebind already-bound loggers).
        from amc.core import sweepers as sweepers_mod

        with patch.object(sweepers_mod._log, "warning") as mock_warning:
            async with session_factory() as session:
                deleted, skipped = await sweep_attachments(
                    session,
                    retention_days=90,
                    now=clock.now,
                )

        # Counted as deleted (post-state matches), no skip.
        assert (deleted, skipped) == (1, 0)
        # Row was still nulled out so future sweeps don't re-check it.
        row = await _fetch_attachment(session_factory, att_id)
        assert row is not None
        assert row["bytes_path"] is None
        # WARN logged with the canonical event name and the row's identifiers.
        mock_warning.assert_called_once()
        call_args = mock_warning.call_args
        assert call_args.args[0] == "attachment_bytes_missing_on_sweep"
        assert call_args.kwargs["attachment_id"] == att_id
        assert call_args.kwargs["bytes_path"] == str(ghost_path)

    async def test_permission_denied_logs_error_and_skips_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        """Unlink raises PermissionError → log ERROR, skip row, counted in skipped."""

        await _seed_message_chain(session_factory)
        att_id = "att_LOCKED000000000000000000000"[:30]
        file_path = _write_bytes(store_dir, att_id)
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now - timedelta(days=100),
            bytes_path=str(file_path),
        )

        def _denied(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("operation not permitted")

        from amc.core import sweepers as sweepers_mod

        with (
            patch("amc.core.sweepers.os.unlink", side_effect=_denied),
            patch.object(sweepers_mod._log, "error") as mock_error,
        ):
            async with session_factory() as session:
                deleted, skipped = await sweep_attachments(
                    session,
                    retention_days=90,
                    now=clock.now,
                )

        assert (deleted, skipped) == (0, 1)
        # Row preserved unchanged.
        row = await _fetch_attachment(session_factory, att_id)
        assert row is not None
        assert row["bytes_path"] == str(file_path)
        # File still on disk (the unlink was patched).
        assert file_path.exists()
        # ERROR logged with canonical event name.
        mock_error.assert_called_once()
        call_args = mock_error.call_args
        assert call_args.args[0] == "attachment_sweep_permission_denied"
        assert call_args.kwargs["attachment_id"] == att_id
        assert call_args.kwargs["bytes_path"] == str(file_path)

    async def test_single_failure_does_not_abort_sweep(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        """One PermissionError must not block the rest of the rows."""

        await _seed_message_chain(session_factory)

        # Three eligible rows. Patch unlink to fail only on the middle one.
        ids: list[str] = []
        paths: list[Path] = []
        for i in range(3):
            att_id = f"att_BATCH{i}_______________________"[:30]
            ids.append(att_id)
            paths.append(_write_bytes(store_dir, att_id, f"payload-{i}".encode()))
            await _insert_attachment(
                session_factory,
                attachment_id=att_id,
                created_at=clock.now - timedelta(days=100 + i),
                bytes_path=str(paths[-1]),
                size_bytes=len(f"payload-{i}".encode()),
            )

        bad_path = str(paths[1])
        real_unlink = __import__("os").unlink

        def _selective(path: str) -> None:
            if path == bad_path:
                raise PermissionError("locked")
            real_unlink(path)

        with patch("amc.core.sweepers.os.unlink", side_effect=_selective):
            async with session_factory() as session:
                deleted, skipped = await sweep_attachments(
                    session,
                    retention_days=90,
                    now=clock.now,
                )

        assert (deleted, skipped) == (2, 1)

        # First and third row swept; middle row preserved.
        first_row = await _fetch_attachment(session_factory, ids[0])
        middle_row = await _fetch_attachment(session_factory, ids[1])
        last_row = await _fetch_attachment(session_factory, ids[2])
        assert first_row is not None and first_row["bytes_path"] is None
        assert middle_row is not None and middle_row["bytes_path"] == bad_path
        assert last_row is not None and last_row["bytes_path"] is None
        # Files: first + last unlinked, middle still on disk.
        assert not paths[0].exists()
        assert paths[1].exists()
        assert not paths[2].exists()

    async def test_clock_fast_forward_triggers_sweep(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        """Row fresh now; same row eligible after clock advances past retention."""

        await _seed_message_chain(session_factory)
        att_id = "att_TTL90000000000000000000000"
        file_path = _write_bytes(store_dir, att_id)
        # Created exactly at clock.now → 0 days old.
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now,
            bytes_path=str(file_path),
        )

        # Just before threshold → still fresh.
        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                retention_days=90,
                now=clock.now + timedelta(days=89),
            )
        assert (deleted, skipped) == (0, 0)
        assert file_path.exists()

        # Fast-forward past the retention window.
        clock.advance(timedelta(days=91))
        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                retention_days=90,
                now=clock.now,
            )
        assert (deleted, skipped) == (1, 0)
        assert not file_path.exists()
        row = await _fetch_attachment(session_factory, att_id)
        assert row is not None
        assert row["bytes_path"] is None

    async def test_default_now_uses_wall_clock(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store_dir: Path,
    ) -> None:
        """Omitting ``now`` falls back to wall-clock UTC."""

        await _seed_message_chain(session_factory)
        att_id = "att_ANCIENT0000000000000000000"
        file_path = _write_bytes(store_dir, att_id)
        # Created in the year 2000 — easily past any retention.
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
            bytes_path=str(file_path),
        )

        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(session, retention_days=90)
        assert (deleted, skipped) == (1, 0)
        assert not file_path.exists()


# ---------------------------------------------------------------------------
# Integration with GET /attachments/{id}
# ---------------------------------------------------------------------------


class TestSweepIntegratesWithReadPath:
    async def test_swept_row_returns_none_from_load(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        """After sweep, ``_load_attachment`` returns ``None`` (→ 404 in route)."""

        from amc.api.attachments_get import _load_attachment

        await _seed_message_chain(session_factory)
        att_id = "att_INTEG000000000000000000000"
        file_path = _write_bytes(store_dir, att_id)
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now - timedelta(days=100),
            bytes_path=str(file_path),
        )

        # Pre-sweep: route helper finds the row.
        async with session_factory() as session:
            pre = await _load_attachment(session, att_id)
        assert pre is not None
        assert pre["bytes_path"] == str(file_path)

        # Sweep.
        async with session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                retention_days=90,
                now=clock.now,
            )
        assert (deleted, skipped) == (1, 0)

        # Post-sweep: route helper returns None → route raises 404.
        async with session_factory() as session:
            post = await _load_attachment(session, att_id)
        assert post is None


# ---------------------------------------------------------------------------
# AttachmentSweeper class
# ---------------------------------------------------------------------------


class TestAttachmentSweeper:
    async def test_tick_returns_deleted_and_skipped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        await _seed_message_chain(session_factory)
        old_id = "att_TICKOLD0000000000000000000"
        fresh_id = "att_TICKFRESH00000000000000000"
        old_path = _write_bytes(store_dir, old_id)
        fresh_path = _write_bytes(store_dir, fresh_id)
        await _insert_attachment(
            session_factory,
            attachment_id=old_id,
            created_at=clock.now - timedelta(days=100),
            bytes_path=str(old_path),
        )
        await _insert_attachment(
            session_factory,
            attachment_id=fresh_id,
            created_at=clock.now - timedelta(days=10),
            bytes_path=str(fresh_path),
        )

        sweeper = AttachmentSweeper(
            session_factory=session_factory,
            time_provider=clock,
            retention_days=90,
        )
        deleted, skipped = await sweeper.tick()
        assert (deleted, skipped) == (1, 0)
        assert not old_path.exists()
        assert fresh_path.exists()

    async def test_tick_logs_attachment_sweep_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        """Per spec/task: ``event=attachment_sweep deleted={n} skipped={m}``."""

        await _seed_message_chain(session_factory)
        att_id = "att_LOGCHECK00000000000000000000"[:30]
        file_path = _write_bytes(store_dir, att_id)
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now - timedelta(days=100),
            bytes_path=str(file_path),
        )

        sweeper = AttachmentSweeper(
            session_factory=session_factory,
            time_provider=clock,
            retention_days=90,
        )

        # Spy on the module-level structlog logger (avoids test-order
        # sensitivity from structlog's lazy-bound proxy).
        from amc.core import sweepers as sweepers_mod

        with patch.object(sweepers_mod._log, "info") as mock_info:
            deleted, skipped = await sweeper.tick()

        assert (deleted, skipped) == (1, 0)
        # The tick must emit exactly one ``attachment_sweep`` event with
        # ``deleted`` and ``skipped`` kwargs (per task acceptance).
        sweep_calls = [
            c for c in mock_info.call_args_list if c.args and c.args[0] == "attachment_sweep"
        ]
        assert sweep_calls, f"no attachment_sweep log; got {mock_info.call_args_list}"
        call = sweep_calls[0]
        assert call.kwargs == {"deleted": 1, "skipped": 0}

    async def test_retention_days_default_is_90(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unset env → DEFAULT_ATTACHMENT_RETENTION_DAYS (90)."""

        monkeypatch.delenv(ENV_ATTACHMENT_RETENTION_DAYS, raising=False)
        sweeper = AttachmentSweeper(session_factory=session_factory)
        assert sweeper.retention_days == DEFAULT_ATTACHMENT_RETENTION_DAYS == 90

    async def test_retention_days_from_env(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``$AMC_ATTACHMENT_RETENTION_DAYS`` overrides the default."""

        monkeypatch.setenv(ENV_ATTACHMENT_RETENTION_DAYS, "30")
        sweeper = AttachmentSweeper(session_factory=session_factory)
        assert sweeper.retention_days == 30

    async def test_explicit_retention_days_wins_over_env(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENV_ATTACHMENT_RETENTION_DAYS, "30")
        sweeper = AttachmentSweeper(session_factory=session_factory, retention_days=7)
        assert sweeper.retention_days == 7

    async def test_invalid_env_raises_value_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENV_ATTACHMENT_RETENTION_DAYS, "not-a-number")
        with pytest.raises(ValueError, match=ENV_ATTACHMENT_RETENTION_DAYS):
            AttachmentSweeper(session_factory=session_factory)

    async def test_non_positive_env_raises_value_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENV_ATTACHMENT_RETENTION_DAYS, "0")
        with pytest.raises(ValueError, match="positive"):
            AttachmentSweeper(session_factory=session_factory)

    async def test_start_is_reentrant(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        sweeper = AttachmentSweeper(
            session_factory=session_factory,
            time_provider=clock,
            sweep_interval_seconds=60.0,
            retention_days=90,
        )
        await sweeper.start()
        first_task = sweeper._task
        await sweeper.start()
        second_task = sweeper._task
        try:
            assert first_task is second_task
        finally:
            await sweeper.stop()

    async def test_stop_cancels_loop_and_is_idempotent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
    ) -> None:
        sweeper = AttachmentSweeper(
            session_factory=session_factory,
            time_provider=clock,
            sweep_interval_seconds=60.0,
            retention_days=90,
        )
        await sweeper.start()
        await sweeper.stop()
        await sweeper.stop()
        assert sweeper._task is None

    async def test_loop_drives_a_tick_within_short_interval(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _FakeClock,
        store_dir: Path,
    ) -> None:
        """With a sub-second interval the loop runs at least one sweep."""

        await _seed_message_chain(session_factory)
        att_id = "att_LOOPDRIVEN0000000000000000"[:30]
        file_path = _write_bytes(store_dir, att_id)
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=clock.now - timedelta(days=100),
            bytes_path=str(file_path),
        )

        sweeper = AttachmentSweeper(
            session_factory=session_factory,
            time_provider=clock,
            sweep_interval_seconds=0.01,
            retention_days=90,
        )
        await sweeper.start()
        await asyncio.sleep(0.05)
        await sweeper.stop()

        # The expired row was swept by the loop.
        row = await _fetch_attachment(session_factory, att_id)
        assert row is not None
        assert row["bytes_path"] is None
        assert not file_path.exists()

    async def test_default_sweep_interval_matches_spec(self) -> None:
        """Spec §10.5 mandates a daily cadence (86400s) for attachment sweep."""

        assert DEFAULT_ATTACHMENT_SWEEP_INTERVAL_SECONDS == 86400.0

    async def test_default_time_provider_is_utcnow(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store_dir: Path,
    ) -> None:
        """Omitting ``time_provider`` defaults to wall-clock UTC."""

        await _seed_message_chain(session_factory)
        att_id = "att_WALLCLOCK0000000000000000"[:30]
        file_path = _write_bytes(store_dir, att_id)
        await _insert_attachment(
            session_factory,
            attachment_id=att_id,
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
            bytes_path=str(file_path),
        )

        sweeper = AttachmentSweeper(
            session_factory=session_factory,
            retention_days=90,
        )
        deleted, skipped = await sweeper.tick()
        assert (deleted, skipped) == (1, 0)
        assert not file_path.exists()
