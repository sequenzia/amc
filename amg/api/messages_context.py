"""``GET /messages/context`` — N-around-target message context (spec §5.4 / §7.4.3).

Public surface
--------------
* :data:`router` — :class:`fastapi.APIRouter` with the ``/messages`` prefix and
  one route (``GET /context``). Mount via ``app.include_router(router)`` once
  the top-level adapter app exists.
* :func:`fetch_context` — pure async function used by the route handler;
  exposed for direct callers and unit tests so they can exercise the SQL path
  without instantiating a FastAPI app.

Behavior (spec §5.4)
--------------------
1. Verify the target ``around_message_id`` exists AND is *visible*. The
   target must have ``allowlist_status='allowed'`` — quarantined
   (``unknown``) and agent-sent (``outbound``) rows cannot be used as the
   anchor (the spec calls out only ``allowed`` for the visible surface and
   the only callers in the wild ask for inbound conversation pivots).
2. Fetch up to ``before`` *visible* messages older than the target
   (``message_ts < target_ts``, ordered DESC, limited).
3. Fetch up to ``after`` *visible* messages newer than the target
   (``message_ts > target_ts``, ordered ASC, limited).
4. Combine target + before + after and return chronologically ordered.

A message is *visible* in the neighbor scan when
``allowlist_status != 'unknown'``. This includes both
``allowlist_status='allowed'`` (inbound from allowlisted senders) and
``allowlist_status='outbound'`` (agent's own replies). The schema CHECK
restricts ``allowlist_status`` to ``('allowed','unknown','outbound')``, so
the inverted filter is closed.

Note on session injection
-------------------------
The route reads its :class:`async_sessionmaker` from
``request.app.state.session_factory`` so tests (and the eventual top-level
adapter app) can wire a tmp_path SQLite DB without touching env vars.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from amg.core.auth import require_agent_id, require_bearer
from amg.core.envelope import (
    Attachment,
    ChannelType,
    Direction,
    Envelope,
    Sender,
    Source,
)
from amg.core.errors import AMGError, ErrorCode
from amg.core.schema import messages, senders

__all__ = ["fetch_context", "router"]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/messages")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_envelope(row: Any) -> Envelope:
    """Build an :class:`Envelope` from a joined ``messages`` + ``senders`` row.

    The ``senders`` columns are aliased by the SELECT so we can pull
    ``display_name`` and ``person_id`` without a name collision against
    ``messages.allowlist_status``.
    """
    attachments_json = row.attachments_json
    if attachments_json:
        raw_attachments = json.loads(attachments_json)
        attachments = [Attachment.model_validate(a) for a in raw_attachments]
    else:
        attachments = []

    raw_json = row.raw_json
    raw = json.loads(raw_json) if raw_json else {}

    sender = Sender(
        id=row.sender_id,
        display_name=row.sender_display_name,
        person_id=row.sender_person_id,
    )

    return Envelope(
        id=row.id,
        source=Source(row.source),
        channel_id=row.channel_id,
        channel_type=ChannelType(row.channel_type),
        sender=sender,
        text=row.text,
        attachments=attachments,
        reply_to=row.reply_to,
        timestamp=row.message_ts,
        direction=Direction(row.direction),
        raw=raw,
    )


def _select_message_with_sender() -> Any:
    """Build the canonical SELECT used by both target and neighbor lookups.

    Joins ``messages`` to ``senders`` on the composite ``(source, sender_id)``
    key and aliases the sender columns so the row tuple has unambiguous
    attribute names.
    """
    return select(
        messages.c.id,
        messages.c.source,
        messages.c.channel_id,
        messages.c.channel_type,
        messages.c.sender_id,
        messages.c.text,
        messages.c.reply_to,
        messages.c.direction,
        messages.c.allowlist_status,
        messages.c.message_ts,
        messages.c.attachments_json,
        messages.c.raw_json,
        senders.c.display_name.label("sender_display_name"),
        senders.c.person_id.label("sender_person_id"),
    ).select_from(
        messages.join(
            senders,
            and_(
                messages.c.source == senders.c.source,
                messages.c.sender_id == senders.c.sender_id,
            ),
        )
    )


# ---------------------------------------------------------------------------
# Pure fetch (callable from tests + the route handler)
# ---------------------------------------------------------------------------


async def fetch_context(
    session: AsyncSession,
    *,
    channel_id: str,
    around_message_id: str,
    before: int,
    after: int,
) -> list[Envelope]:
    """Return ``[before...] + [target] + [after...]`` envelopes, chronologically.

    Raises :class:`AMGError` with :attr:`ErrorCode.MESSAGE_NOT_FOUND` when the
    target id is unknown or its ``allowlist_status`` is not ``'allowed'``
    (quarantined messages are not visible on this surface).
    """
    base = _select_message_with_sender()

    target_stmt = base.where(messages.c.id == around_message_id)
    target_row = (await session.execute(target_stmt)).first()

    if target_row is None or target_row.allowlist_status != "allowed":
        raise AMGError(
            ErrorCode.MESSAGE_NOT_FOUND,
            "No such message, or message is not visible on this surface.",
            details={"message_id": around_message_id},
        )

    if target_row.channel_id != channel_id:
        # Channel mismatch: treat as not-found to avoid leaking the existence
        # of a message in a channel the caller didn't ask about. Spec §5.4
        # says "target not visible" → 404 MESSAGE_NOT_FOUND.
        raise AMGError(
            ErrorCode.MESSAGE_NOT_FOUND,
            "No such message, or message is not visible on this surface.",
            details={"message_id": around_message_id},
        )

    target_ts = target_row.message_ts
    before_rows: list[Any] = []
    after_rows: list[Any] = []

    if before > 0:
        before_stmt = (
            base.where(
                and_(
                    messages.c.channel_id == channel_id,
                    messages.c.allowlist_status != "unknown",
                    messages.c.message_ts < target_ts,
                )
            )
            .order_by(messages.c.message_ts.desc())
            .limit(before)
        )
        before_rows = list((await session.execute(before_stmt)).all())

    if after > 0:
        after_stmt = (
            base.where(
                and_(
                    messages.c.channel_id == channel_id,
                    messages.c.allowlist_status != "unknown",
                    messages.c.message_ts > target_ts,
                )
            )
            .order_by(messages.c.message_ts.asc())
            .limit(after)
        )
        after_rows = list((await session.execute(after_stmt)).all())

    # Combine: oldest before-rows first (we got them DESC, reverse), target,
    # then after-rows in ASC order. The result is chronological.
    combined = list(reversed(before_rows)) + [target_row] + after_rows
    return [_row_to_envelope(r) for r in combined]


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


async def _session_from_app(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a session from
    ``request.app.state.session_factory``.

    Routes that need DB access pull the session via
    ``Annotated[AsyncSession, Depends(_session_from_app)]``. The factory must
    be wired by the app constructor (or a test fixture) before the route is
    served.
    """
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:
        raise RuntimeError(
            "request.app.state.session_factory is not configured. "
            "The adapter app must wire an async_sessionmaker before serving "
            "/messages/context."
        )
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_from_app)]


@router.get(
    "/context",
    dependencies=[Depends(require_bearer), Depends(require_agent_id)],
    response_model=None,  # Envelope serialization is handled inline.
)
async def get_messages_context(
    session: SessionDep,
    channel_id: Annotated[str, Query(min_length=1)],
    around_message_id: Annotated[str, Query(min_length=1)],
    before: Annotated[int, Query(ge=0, le=50)] = 5,
    after: Annotated[int, Query(ge=0, le=50)] = 5,
) -> dict[str, list[dict[str, Any]]]:
    """``GET /messages/context`` — return chronologically ordered context.

    Bearer + ``X-Agent-ID`` are enforced by the router-level ``dependencies``.
    ``before`` and ``after`` are clamped at ``[0, 50]`` by Pydantic; values
    outside the range surface as the standard ``VALIDATION_FAILED`` envelope
    via the global handler in :mod:`amg.core.errors`.
    """
    envelopes = await fetch_context(
        session,
        channel_id=channel_id,
        around_message_id=around_message_id,
        before=before,
        after=after,
    )
    # Inline serialization: mode="json" applies the Envelope's
    # @field_serializer for the Z-suffix timestamp.
    return {"messages": [e.model_dump(mode="json") for e in envelopes]}
