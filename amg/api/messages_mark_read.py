"""``POST /messages/mark_read`` — per-agent read marker.

Spec references: §5.3 (per-agent read state), §7.4.4 (endpoint contract),
§14 OQ-1 (``marked_count`` semantics).

Behavior
--------
For each ``message_id`` in the request body, UPSERT a row into
``message_reads (message_id, agent_id, read_at)``. The ``(message_id,
agent_id)`` composite primary key makes the operation naturally idempotent —
re-marking an already-read message refreshes ``read_at`` to ``now()`` but
does not error.

OQ-1 resolution
---------------
``marked_count`` is the **total number of unique IDs the client submitted
for marking**, not the number of newly inserted rows. This matches the
example in §7.4.4 where two ids are submitted and the response is
``{"marked_count": 2}``. Non-existent message ids are silently skipped at
the storage layer (FK violation is swallowed) but are still counted in the
response — they were "submitted for marking" and the API surface treats the
request as accepted. See ``internal/notes/oq-1-decision.md``.

Wiring
------
The session factory is set once at adapter startup via
:func:`configure_session_factory`. The route looks it up via
:func:`get_session_factory`. Tests call :func:`reset_session_factory` between
runs for isolation. This mirrors the module-cache pattern in
:mod:`amg.core.auth` and :mod:`amg.core.idempotency`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from amg.core.auth import require_agent_id, require_bearer

__all__ = [
    "MarkReadRequest",
    "MarkReadResponse",
    "configure_session_factory",
    "get_session_factory",
    "mark_read",
    "reset_session_factory",
    "router",
]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MarkReadRequest(BaseModel):
    """Body of ``POST /messages/mark_read`` (spec §7.4.4)."""

    message_ids: list[str] = Field(
        ...,
        description="Message ids to mark as read for the requesting agent.",
    )


class MarkReadResponse(BaseModel):
    """Response of ``POST /messages/mark_read`` (spec §7.4.4)."""

    marked_count: int = Field(
        ...,
        ge=0,
        description=(
            "Total number of unique ids submitted for marking. Per OQ-1 this "
            "is NOT the number of rows newly inserted; it is the count of "
            "ids the client asked us to mark, after de-duplication."
        ),
    )


# ---------------------------------------------------------------------------
# Module-level session factory cache
# ---------------------------------------------------------------------------


_session_factory: async_sessionmaker | None = None


def configure_session_factory(factory: async_sessionmaker) -> None:
    """Install the process-wide :class:`async_sessionmaker` for the route."""

    global _session_factory
    _session_factory = factory


def get_session_factory() -> async_sessionmaker:
    """Return the configured session factory, raising if startup hasn't run."""

    if _session_factory is None:
        raise RuntimeError(
            "Session factory not configured; call configure_session_factory() at adapter startup."
        )
    return _session_factory


def reset_session_factory() -> None:
    """Drop the cached session factory. Intended for test isolation."""

    global _session_factory
    _session_factory = None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


router = APIRouter()


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with millisecond precision.

    The schema's other ``server_default`` columns use SQLite's
    ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` shape — three-digit fractional
    seconds, ``Z`` suffix. We mirror that here so reads and writes are
    consistent regardless of which path generated the value.
    """
    now = datetime.now(UTC)
    # `%f` gives microseconds; spec wants millis-with-Z to match the column
    # format. Slice to three digits.
    return now.strftime("%Y-%m-%dT%H:%M:") + f"{now.second:02d}.{now.microsecond // 1000:03d}Z"


@router.post(
    "/messages/mark_read",
    response_model=MarkReadResponse,
    dependencies=[Depends(require_bearer)],
    summary="Mark messages as read for the requesting agent",
)
async def mark_read(
    payload: MarkReadRequest,
    agent_id: Annotated[str, Depends(require_agent_id)],
) -> MarkReadResponse:
    """Mark each id in ``payload.message_ids`` as read for ``agent_id``.

    UPSERTs ``(message_id, agent_id, read_at=now)`` into ``message_reads``.
    The composite PK on ``(message_id, agent_id)`` makes the operation
    idempotent at the row level; the response ``marked_count`` reports the
    number of unique ids the client submitted (OQ-1).

    Non-existent message ids are silently skipped at the storage layer (the
    FK on ``message_reads.message_id`` rejects them) but are still counted
    in ``marked_count`` because the client "submitted them for marking"
    (OQ-1 resolution).

    Empty ``message_ids`` returns ``{marked_count: 0}`` without touching the
    DB.
    """
    factory = get_session_factory()

    if not payload.message_ids:
        return MarkReadResponse(marked_count=0)

    # Deduplicate input ids: callers may submit the same id twice (e.g., two
    # batches that overlap) and we only want to count it once. Preserve order
    # for deterministic logging.
    seen: set[str] = set()
    unique_ids: list[str] = []
    for mid in payload.message_ids:
        if mid not in seen:
            seen.add(mid)
            unique_ids.append(mid)

    now_iso = _now_iso()

    # SQLite UPSERT (spec §7.3.3 / §7.4.4): ON CONFLICT on the composite
    # primary key refreshes ``read_at``. The query is naturally idempotent
    # because the PK collision is handled in the same statement.
    upsert_sql = text(
        "INSERT INTO message_reads (message_id, agent_id, read_at) "
        "VALUES (:message_id, :agent_id, :read_at) "
        "ON CONFLICT(message_id, agent_id) DO UPDATE SET "
        "read_at = excluded.read_at"
    )

    # Per OQ-1 we count the unique ids the client submitted regardless of
    # whether the underlying row existed. The DB write is best-effort: each
    # id gets its own savepoint so a single FK violation does not poison
    # the rest of the batch.
    async with factory() as session:
        for mid in unique_ids:
            savepoint = await session.begin_nested()
            try:
                await session.execute(
                    upsert_sql,
                    {"message_id": mid, "agent_id": agent_id, "read_at": now_iso},
                )
                await savepoint.commit()
            except Exception:
                # FK violation (id not in messages) or any other row-level
                # error — silently skip per OQ-1 resolution. Roll back the
                # savepoint so the outer transaction stays usable for
                # subsequent ids.
                await savepoint.rollback()
        await session.commit()

    return MarkReadResponse(marked_count=len(unique_ids))
