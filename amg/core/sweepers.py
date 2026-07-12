"""Periodic background sweepers for time-bounded rows in the AMG store.

This module owns two scheduled DELETE jobs:

* the **idempotency-key sweeper** (spec §10.5 row "Idempotency-key sweeper"
  / §7.3.3 ``idempotency_keys.expires_at``) — hourly cadence;
* the **attachment-retention sweeper** (spec §10.5 row "Attachment
  retention sweeper" / §7.3.3 ``attachments.created_at`` +
  ``attachments.bytes_path``) — daily cadence, also unlinks the on-disk
  bytes and nulls ``bytes_path`` so ``GET /attachments/{id}`` returns 404
  after retention.

Why a separate module from :mod:`amg.core.idempotency` and
:mod:`amg.core.attachments`?
----------------------------------------------------------
:class:`amg.core.idempotency.IdempotencyStore` already has a
``sweep_expired()`` method that the FastAPI request path can call
opportunistically; :class:`amg.core.attachments.AttachmentStore` owns the
write path (re-host) for inbound bytes. The sweepers here are the
*scheduled* counterparts — long-lived async tasks that fire on a fixed
cadence regardless of request volume so expired rows don't pile up on a
quiet adapter. Keeping the schedulers in their own module:

* avoids growing ``idempotency.py`` / ``attachments.py`` with periodic-job
  scaffolding;
* gives every future periodic DELETE a single, obvious home; and
* lets the existing modules stay focused on the request-path semantics
  that ``message-sink``, the FastAPI route handlers, and the connectors
  depend on.

Public surface
--------------
* :func:`sweep_idempotency_keys` — one-shot helper for the idempotency
  table. Takes an open :class:`AsyncSession` so callers (tests, ad-hoc
  admin scripts, future REST admin endpoint) can run a sweep inline
  without spinning up the worker.
* :class:`IdempotencySweeper` — async background task mirroring the
  :class:`amg.core.webhook.WebhookWorker` lifecycle (``start`` /
  ``stop`` / ``tick``).
* :func:`sweep_attachments` — one-shot helper for the attachments table.
  For each row whose ``created_at`` is older than ``retention_days`` and
  whose ``bytes_path`` is non-NULL: deletes the file at ``bytes_path``
  and updates the row to ``bytes_path = NULL``. Other columns are
  preserved. Returns ``(deleted, skipped)``.
* :class:`AttachmentSweeper` — daily async background task mirroring
  :class:`IdempotencySweeper`'s lifecycle. Reads
  ``$AMG_ATTACHMENT_RETENTION_DAYS`` (default 90) at construction time.

Design notes
------------
* The idempotency DELETE uses ``expires_at < :now`` — strictly less-than.
  A row whose ``expires_at`` equals the current wall-clock instant is
  still considered in-window for that final tick. The store's
  request-path lookup uses ``expires_at <= :now`` (treats the boundary as
  expired) — the asymmetry is deliberate: a sweeper running on the
  boundary should defer to the read path's stricter check rather than
  racing to delete a row the very moment it might be read.
* The attachment retention sweeper is a row-by-row update (not a bulk
  DELETE) because each row has a side effect on the filesystem that may
  fail independently. A permission-denied unlink on one file must not
  abort the whole sweep — we log ERROR for that row and continue.
* ``time_provider`` is injectable so tests fast-forward past the poll
  interval without sleeping; it returns a tz-aware UTC ``datetime``.
* Both workers are stateless — all state lives in their respective
  tables. On ``start()`` they begin ticking and naturally clean whatever
  is already past threshold.
* Each sweep tick is wrapped in ``except Exception`` so a transient
  SQLite error never tears down the background loop. The error is logged
  and the next tick retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "DEFAULT_ATTACHMENT_RETENTION_DAYS",
    "DEFAULT_ATTACHMENT_SWEEP_INTERVAL_SECONDS",
    "DEFAULT_SWEEP_INTERVAL_SECONDS",
    "ENV_ATTACHMENT_RETENTION_DAYS",
    "AttachmentSweeper",
    "IdempotencySweeper",
    "sweep_attachments",
    "sweep_idempotency_keys",
]

# Hourly cadence per spec §10.5 ("Hourly delete of expired keys").
DEFAULT_SWEEP_INTERVAL_SECONDS: Final[float] = 3600.0

# Daily cadence per spec §10.5 ("Daily delete of attachments older than 90 days").
DEFAULT_ATTACHMENT_SWEEP_INTERVAL_SECONDS: Final[float] = 86400.0

# Default attachment retention window (days). Mirrored in `.env.example` and
# spec §11.2 (`AMG_ATTACHMENT_RETENTION_DAYS=90`).
DEFAULT_ATTACHMENT_RETENTION_DAYS: Final[int] = 90

# Env-var name; mirrored in `.env.example` and spec §11.2.
ENV_ATTACHMENT_RETENTION_DAYS: Final[str] = "AMG_ATTACHMENT_RETENTION_DAYS"

# ISO 8601 with millisecond precision and trailing ``Z`` — matches the format
# written by :class:`amg.core.idempotency.IdempotencyStore` so lexicographic
# string comparison in SQLite ordering matches chronological order.
_ISO_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S.%fZ"


_log = structlog.get_logger("amg.sweepers")


def _utcnow() -> datetime:
    """Default time provider — UTC wall clock, tz-aware."""

    return datetime.now(tz=UTC)


def _format_ts(ts: datetime) -> str:
    """Render a UTC datetime in the project's canonical ISO 8601 form.

    Naive datetimes are assumed to already be UTC (consistent with
    :func:`amg.core.idempotency._format_ts`).
    """

    aware = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return aware.strftime(_ISO_FORMAT)


# ---------------------------------------------------------------------------
# One-shot helper
# ---------------------------------------------------------------------------


async def sweep_idempotency_keys(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Delete every ``idempotency_keys`` row whose ``expires_at`` is in the past.

    Parameters
    ----------
    session
        An open :class:`AsyncSession`. The caller owns the session's
        lifetime; this function executes the DELETE and commits.
    now
        Wall-clock instant to compare against ``expires_at``. Defaults to
        :func:`datetime.now(tz=UTC)`. Tests pass a fixed value to drive
        deterministic behavior.

    Returns
    -------
    int
        Number of rows deleted (0 when nothing was expired).

    Notes
    -----
    Uses strict ``<`` so a row whose ``expires_at`` exactly equals ``now``
    survives this sweep — the read path's ``<=`` check is the authority on
    boundary handling. Empty table is a no-op (returns 0). The operation is
    idempotent: re-running with the same ``now`` deletes nothing because the
    expired rows are already gone.
    """

    instant = now if now is not None else _utcnow()
    now_iso = _format_ts(instant)
    result = await session.execute(
        text("DELETE FROM idempotency_keys WHERE expires_at < :now"),
        {"now": now_iso},
    )
    await session.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class IdempotencySweeper:
    """Hourly async task that deletes expired ``idempotency_keys`` rows.

    Mirrors :class:`amg.core.webhook.WebhookWorker`: ``start()`` schedules
    the loop on the running event loop, ``stop()`` cancels it. Tests usually
    drive the sweeper via :meth:`tick` directly instead of running the loop
    so they don't depend on real time passing.
    """

    __slots__ = (
        "_session_factory",
        "_time_provider",
        "_sweep_interval_seconds",
        "_task",
        "_stop_event",
    )

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        time_provider: Callable[[], datetime] = _utcnow,
        sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._time_provider = time_provider
        self._sweep_interval_seconds = sweep_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Schedule the periodic sweep loop on the running event loop.

        Re-entrant: calling ``start()`` again while the worker is running
        is a no-op so adapter startup retries don't spawn duplicate tasks.
        """

        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="amg-idempotency-sweeper")
        _log.info("idempotency_sweeper_started", interval_s=self._sweep_interval_seconds)

    async def stop(self) -> None:
        """Cancel the loop and wait for it to exit cleanly."""

        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        _log.info("idempotency_sweeper_stopped")

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Polling loop. Sweeps once, then waits ``sweep_interval_seconds``.

        ``stop()`` interrupts the wait so shutdown is prompt regardless of
        the configured interval.
        """

        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                _log.error("idempotency_sweeper_tick_error", exc_info=exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._sweep_interval_seconds,
                )

    async def tick(self) -> int:
        """Run one sweep and emit the structured-log event.

        Returns the number of rows deleted so callers (and tests) can
        assert on the outcome of a single pass.
        """

        async with self._session_factory() as session:
            deleted = await sweep_idempotency_keys(session, now=self._time_provider())
        _log.info("idempotency_sweep", deleted=deleted)
        return deleted


# ---------------------------------------------------------------------------
# Attachment retention — one-shot helper
# ---------------------------------------------------------------------------


def _resolve_retention_days(override: int | None) -> int:
    """Pick the retention window from override → env → default.

    Mirrors the env-resolution pattern used by
    :class:`amg.core.attachments.AttachmentStore` so operators get a single,
    consistent precedence rule across the attachment subsystem.
    """

    if override is not None:
        return override
    raw = os.environ.get(ENV_ATTACHMENT_RETENTION_DAYS)
    if raw is None or not raw.strip():
        return DEFAULT_ATTACHMENT_RETENTION_DAYS
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ENV_ATTACHMENT_RETENTION_DAYS}={raw!r} is not a valid integer; "
            f"expected a positive number of days."
        ) from exc
    if parsed <= 0:
        raise ValueError(
            f"{ENV_ATTACHMENT_RETENTION_DAYS}={raw!r} must be a positive integer."
        )
    return parsed


async def sweep_attachments(
    session: AsyncSession,
    retention_days: int,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Retention-sweep the ``attachments`` table.

    For each row whose ``created_at`` is older than ``now - retention_days``
    AND whose ``bytes_path IS NOT NULL``: deletes the file at
    ``bytes_path`` and updates the row to ``bytes_path = NULL``. All other
    columns are preserved (so the message still has its attachment-id
    history; the bytes are simply gone). Rows with ``bytes_path IS NULL``
    are not touched — the bytes were already swept (or never re-hosted).

    Parameters
    ----------
    session
        An open :class:`AsyncSession`. The caller owns the session's
        lifetime; this function commits after each row update so a
        permission-denied unlink mid-sweep does not lose the rows that
        already succeeded.
    retention_days
        Threshold in days. A row is eligible when ``created_at < now -
        retention_days``. Strictly less-than: a row created exactly at the
        boundary is preserved for one more tick.
    now
        Wall-clock instant to compare against ``created_at``. Defaults to
        :func:`datetime.now(tz=UTC)`. Tests pass a fixed value to drive
        deterministic behavior.

    Returns
    -------
    tuple[int, int]
        ``(deleted, skipped)``. ``deleted`` counts rows whose bytes were
        unlinked (or were already gone — the row is still nulled out so
        retention is idempotent). ``skipped`` counts rows where the unlink
        failed with a permission error and the row was left untouched.

    Notes
    -----
    * **Missing file on disk** is treated as a successful sweep: the bytes
      are already gone, so the row is updated to ``bytes_path = NULL``
      and counted in ``deleted``. A WARN is logged so the operator knows
      something pruned the bytes out from under the adapter.
    * **Permission denied** on unlink logs ERROR and skips that row (the
      row stays unchanged, counted in ``skipped``). The next sweep will
      retry the same row.
    * **Idempotent**: re-running with the same ``now`` is a no-op because
      the rows that were swept now have ``bytes_path IS NULL`` and no
      longer match the WHERE clause.
    * Each row is processed in its own commit so a single failure cannot
      roll back the rows that already succeeded.
    """

    instant = now if now is not None else _utcnow()
    threshold = instant - timedelta(days=retention_days)
    threshold_iso = _format_ts(threshold)

    # Snapshot the eligible rows up front so the iteration is bounded —
    # the row updates we do during the loop also remove rows from this
    # query's result set, which a server-side cursor would handle
    # correctly but a sync-rendered list expresses more clearly.
    result = await session.execute(
        text(
            "SELECT id, bytes_path FROM attachments "
            "WHERE created_at < :threshold AND bytes_path IS NOT NULL"
        ),
        {"threshold": threshold_iso},
    )
    rows = result.all()

    deleted = 0
    skipped = 0

    for attachment_id, bytes_path in rows:
        try:
            os.unlink(bytes_path)
        except FileNotFoundError:
            # Bytes already gone — null out the row anyway so future sweeps
            # don't keep re-checking. Counted as "deleted" because the
            # post-state matches what a successful sweep produces.
            _log.warning(
                "attachment_bytes_missing_on_sweep",
                attachment_id=attachment_id,
                bytes_path=bytes_path,
            )
        except PermissionError as exc:
            # Operator-fixable problem. Log ERROR, skip the row, continue
            # the sweep so one bad file does not block the rest.
            _log.error(
                "attachment_sweep_permission_denied",
                attachment_id=attachment_id,
                bytes_path=bytes_path,
                error=str(exc),
            )
            skipped += 1
            continue
        except OSError as exc:
            # Treat any other OS-level failure (EIO, etc.) the same as a
            # permission error — log ERROR, skip, retry next sweep.
            _log.error(
                "attachment_sweep_unlink_failed",
                attachment_id=attachment_id,
                bytes_path=bytes_path,
                errno=exc.errno,
                error=str(exc),
            )
            skipped += 1
            continue

        await session.execute(
            text("UPDATE attachments SET bytes_path = NULL WHERE id = :id"),
            {"id": attachment_id},
        )
        await session.commit()
        deleted += 1

    return deleted, skipped


# ---------------------------------------------------------------------------
# Attachment retention — worker
# ---------------------------------------------------------------------------


class AttachmentSweeper:
    """Daily async task that deletes retention-expired attachment bytes.

    Mirrors :class:`IdempotencySweeper`: ``start()`` schedules the loop on
    the running event loop, ``stop()`` cancels it. Tests usually drive the
    sweeper via :meth:`tick` directly instead of running the loop so they
    don't depend on real time passing.

    The retention window comes from
    ``$AMG_ATTACHMENT_RETENTION_DAYS`` (default ``90``) unless an explicit
    ``retention_days=`` is passed.
    """

    __slots__ = (
        "_session_factory",
        "_time_provider",
        "_sweep_interval_seconds",
        "_retention_days",
        "_task",
        "_stop_event",
    )

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        time_provider: Callable[[], datetime] = _utcnow,
        sweep_interval_seconds: float = DEFAULT_ATTACHMENT_SWEEP_INTERVAL_SECONDS,
        retention_days: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._time_provider = time_provider
        self._sweep_interval_seconds = sweep_interval_seconds
        self._retention_days = _resolve_retention_days(retention_days)
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def retention_days(self) -> int:
        """Resolved retention window in days (read-only)."""
        return self._retention_days

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Schedule the periodic sweep loop on the running event loop.

        Re-entrant: calling ``start()`` again while the worker is running
        is a no-op so adapter startup retries don't spawn duplicate tasks.
        """

        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="amg-attachment-sweeper")
        _log.info(
            "attachment_sweeper_started",
            interval_s=self._sweep_interval_seconds,
            retention_days=self._retention_days,
        )

    async def stop(self) -> None:
        """Cancel the loop and wait for it to exit cleanly."""

        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        _log.info("attachment_sweeper_stopped")

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Polling loop. Sweeps once, then waits ``sweep_interval_seconds``.

        ``stop()`` interrupts the wait so shutdown is prompt regardless of
        the configured interval.
        """

        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                _log.error("attachment_sweeper_tick_error", exc_info=exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._sweep_interval_seconds,
                )

    async def tick(self) -> tuple[int, int]:
        """Run one sweep and emit the structured-log event.

        Returns ``(deleted, skipped)`` so callers (and tests) can assert
        on the outcome of a single pass.
        """

        async with self._session_factory() as session:
            deleted, skipped = await sweep_attachments(
                session,
                self._retention_days,
                now=self._time_provider(),
            )
        _log.info("attachment_sweep", deleted=deleted, skipped=skipped)
        return deleted, skipped
