"""``GET /messages/quarantine`` — operator review of non-allowlisted inbound messages
(spec §5.7, §5.9, §7.4.8).

Returns the slice of messages whose ``allowlist_status='unknown'`` — i.e.
inbound messages from senders not on the allowlist. This endpoint is **global**
(not per-agent): the operator reviews all quarantined traffic in one place.
``X-Agent-ID`` is therefore not consulted; only the bearer token is required.

Public surface
--------------
* :data:`router` — :class:`fastapi.APIRouter` with prefix ``/messages``.
* :func:`configure_session_factory` — startup hook that hands the route a bound
  ``async_sessionmaker``. The route resolves it lazily so tests (and the main
  app) can swap the factory after import.
* :func:`get_session_factory` — accessor used by the route; raises if the
  factory has not been configured.

Notes
-----
* Ordering is ``message_ts DESC`` (newest first) — operator review starts with
  the latest unknown sender.
* Envelope construction is inlined here on purpose — sibling endpoint tasks in
  this wave each own their own assembly to avoid concurrent edit contention on
  a shared helper module.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from amc.core.auth import require_bearer
from amc.core.envelope import (
    Attachment,
    ChannelType,
    Direction,
    Envelope,
    Sender,
    Source,
)
from amc.core.errors import AMCError, ErrorCode

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "configure_session_factory",
    "get_session_factory",
    "reset_session_factory",
    "router",
]

DEFAULT_LIMIT: Final[int] = 20
MAX_LIMIT: Final[int] = 100

router = APIRouter(prefix="/messages", tags=["messages"])

# Module-level session factory cache. Set once at startup by the app wiring
# via :func:`configure_session_factory`; tests may override per-test.
_session_factory: async_sessionmaker | None = None


def configure_session_factory(factory: async_sessionmaker) -> None:
    """Register the async session factory the route should use."""
    global _session_factory
    _session_factory = factory


def get_session_factory() -> async_sessionmaker:
    """Return the configured session factory, raising if unset."""
    if _session_factory is None:
        raise RuntimeError(
            "messages_quarantine session factory has not been configured. "
            "Call configure_session_factory() at startup."
        )
    return _session_factory


def reset_session_factory() -> None:
    """Clear the cached session factory. Intended for test isolation."""
    global _session_factory
    _session_factory = None


# ---------------------------------------------------------------------------
# Query parameter parsing
# ---------------------------------------------------------------------------


def _parse_since(raw: str | None) -> str | None:
    """Validate ``since`` is RFC 3339 and return the canonical UTC string.

    Returns ``None`` if ``raw`` is unset. Raises :class:`AMCError`
    (``VALIDATION_FAILED``) on bad format. The canonical form ends in ``Z`` so
    SQL string comparison against the ``message_ts`` column (stored as ISO
    8601 UTC text) is correct.
    """
    if raw is None:
        return None
    try:
        # ``fromisoformat`` in 3.11+ accepts ``Z`` suffix and ``+HH:MM`` offsets.
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AMCError(
            code=ErrorCode.VALIDATION_FAILED,
            message="`since` must be an RFC 3339 timestamp.",
            details={"field": "since", "value": raw, "reason": str(exc)},
        ) from exc

    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return parsed.isoformat().replace("+00:00", "Z")


def _validate_limit(limit: int) -> int:
    """Clamp ``limit`` to ``[1, MAX_LIMIT]``; reject ``< 1`` as ``VALIDATION_FAILED``."""
    if limit < 1:
        raise AMCError(
            code=ErrorCode.VALIDATION_FAILED,
            message="`limit` must be >= 1.",
            details={"field": "limit", "value": limit},
        )
    return min(limit, MAX_LIMIT)


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


def _row_to_envelope(row: Any) -> Envelope:
    """Build an :class:`Envelope` from a joined ``messages`` + ``senders`` row."""
    attachments: list[Attachment] = []
    if row.attachments_json:
        try:
            parsed = json.loads(row.attachments_json)
        except (json.JSONDecodeError, TypeError):
            parsed = []
        for item in parsed:
            attachments.append(Attachment(**item))

    raw: dict[str, Any] = {}
    if row.raw_json:
        try:
            decoded = json.loads(row.raw_json)
            if isinstance(decoded, dict):
                raw = decoded
        except (json.JSONDecodeError, TypeError):
            raw = {}

    return Envelope(
        id=row.id,
        source=Source(row.source),
        channel_id=row.channel_id,
        channel_type=ChannelType(row.channel_type),
        sender=Sender(
            id=row.sender_id,
            display_name=row.sender_display_name,
            person_id=row.sender_person_id,
        ),
        text=row.text,
        attachments=attachments,
        reply_to=row.reply_to,
        timestamp=row.message_ts,
        direction=Direction(row.direction),
        raw=raw,
    )


# Column list shared by the SELECT and ``_row_to_envelope``. Aliased so we can
# read ``row.sender_display_name`` etc. without colliding with the join key
# ``sender_id``.
_SELECT_COLUMNS: Final[str] = """
    m.id AS id,
    m.source AS source,
    m.channel_id AS channel_id,
    m.channel_type AS channel_type,
    m.sender_id AS sender_id,
    m.text AS text,
    m.reply_to AS reply_to,
    m.direction AS direction,
    m.allowlist_status AS allowlist_status,
    m.message_ts AS message_ts,
    m.attachments_json AS attachments_json,
    m.raw_json AS raw_json,
    s.display_name AS sender_display_name,
    s.person_id AS sender_person_id
"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/quarantine",
    dependencies=[Depends(require_bearer)],
    summary="List quarantined (non-allowlisted) inbound messages",
)
async def list_quarantined_messages(
    since: Annotated[
        str | None,
        Query(description="RFC 3339 timestamp; only return messages newer than this."),
    ] = None,
    limit: Annotated[
        int,
        Query(description="Max envelopes to return (default 20, max 100)."),
    ] = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return inbound messages with ``allowlist_status='unknown'``.

    Per spec §7.4.8 the response shape is::

        {"messages": [envelope, ...], "next_since": "<iso8601>"}

    Ordering is ``message_ts DESC`` (newest first) so operators see the most
    recent quarantine entries first. ``next_since`` is the maximum
    ``message_ts`` of returned envelopes, falling back to the input ``since``
    (or an empty string if ``since`` was also unset) on an empty result.
    """
    canonical_since = _parse_since(since)
    effective_limit = _validate_limit(limit)

    sql = text(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM messages m
        JOIN senders s
          ON s.source = m.source AND s.sender_id = m.sender_id
        WHERE m.allowlist_status = 'unknown'
          AND (:since IS NULL OR m.message_ts > :since)
        ORDER BY m.message_ts DESC
        LIMIT :limit
        """
    ).bindparams(
        bindparam("since", value=canonical_since),
        bindparam("limit", value=effective_limit),
    )

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(sql)
        rows = result.mappings().all()

    envelopes = [_row_to_envelope(_AttrRow(r)) for r in rows]

    if envelopes:
        next_since = max(env.timestamp for env in envelopes)
        next_since_str = next_since.isoformat().replace("+00:00", "Z")
    else:
        # Echo the (canonical) input on empty result. ``""`` keeps the field
        # present so consumers don't have to special-case ``None``.
        next_since_str = canonical_since or ""

    return {
        "messages": [env.model_dump(mode="json") for env in envelopes],
        "next_since": next_since_str,
    }


class _AttrRow:
    """Tiny attribute-access wrapper over a SQLAlchemy ``RowMapping``.

    ``_row_to_envelope`` is written against attribute access (``row.id``,
    ``row.text``) so it stays readable. ``Result.mappings()`` yields dict-like
    rows, so we wrap them here rather than scatter ``row["..."]`` calls.
    """

    __slots__ = ("_data",)

    def __init__(self, mapping: Any) -> None:
        self._data = mapping

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
