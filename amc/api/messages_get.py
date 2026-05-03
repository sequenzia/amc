"""``GET /messages/{id}`` — single-message lookup with allowlist visibility.

Spec reference: §7.4.2.

Behavior:

* Both ``Authorization: Bearer ...`` and ``X-Agent-ID: <name>`` are required
  (per §7.4 header table). Wired via ``Depends(require_bearer)`` and
  ``Depends(require_agent_id)``.
* Look up the row by ``messages.id``. If the row is missing, OR its
  ``allowlist_status`` is ``'unknown'`` (i.e. quarantined inbound), respond
  ``404 MESSAGE_NOT_FOUND`` — the spec deliberately conflates "not found" and
  "not visible" so the caller cannot probe for the existence of quarantined
  messages.
* Outbound messages have ``allowlist_status='outbound'`` and are always
  visible — they were sent by the agent itself.
* Inbound messages with ``allowlist_status='allowed'`` are visible.
* On hit, the row is hydrated into the canonical :class:`Envelope` shape and
  returned. The same hydration path the webhook worker uses (joining
  ``senders`` for ``display_name`` / ``person_id``, decoding
  ``attachments_json`` and ``raw_json``) is reused here so the wire shape
  stays identical to outbound webhook deliveries.

Wiring (#41 ``amc/app.py``):

* The session factory is installed at startup via
  :func:`configure_session_factory`; the route reads it through
  :func:`get_configured_session_factory`. This mirrors the
  ``configure_idempotency_store`` / ``configure_bearer_token`` pattern used
  elsewhere so tests don't need to monkey with ``app.dependency_overrides``.
* The router is mounted into the app with ``app.include_router(router)``.
* The ``MessageId`` regex on the path param prevents the ``{id}`` route from
  matching sibling routes like ``/messages/unread`` and ``/messages/context``
  even if mount order changes.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from amc.core.auth import require_agent_id, require_bearer
from amc.core.envelope import Envelope, MessageId
from amc.core.errors import AMCError, ErrorCode

__all__ = [
    "configure_session_factory",
    "get_configured_session_factory",
    "reset_session_factory",
    "router",
]


# ---------------------------------------------------------------------------
# Session-factory holder
# ---------------------------------------------------------------------------

# Module-level holder so the route can resolve the configured session factory
# without callers having to thread it through every ``Depends``. Set via
# :func:`configure_session_factory` at app startup; reset for tests via
# :func:`reset_session_factory`.
_configured_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure_session_factory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Install the process-wide async session factory for the route."""
    global _configured_session_factory
    _configured_session_factory = session_factory


def get_configured_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the configured session factory, raising if startup hasn't run."""
    if _configured_session_factory is None:
        raise RuntimeError(
            "Session factory not configured; call configure_session_factory() at startup."
        )
    return _configured_session_factory


def reset_session_factory() -> None:
    """Drop any configured session factory. Used by tests for isolation."""
    global _configured_session_factory
    _configured_session_factory = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/messages")


_MessageIdPath = Annotated[
    MessageId,
    Path(
        description="ULID-prefixed message id (``msg_<26 Crockford-base32 chars>``).",
    ),
]


@router.get(
    "/{message_id}",
    dependencies=[Depends(require_bearer), Depends(require_agent_id)],
    response_model=Envelope,
    response_model_exclude_none=False,
)
async def get_message(message_id: _MessageIdPath) -> Envelope:
    """Return the single message identified by ``message_id``.

    Visibility rules:

    * ``allowlist_status='allowed'`` → visible (typical inbound).
    * ``allowlist_status='outbound'`` → visible (agent-sent).
    * ``allowlist_status='unknown'`` → hidden (return 404).
    * Row missing → 404.

    The two "not visible" cases collapse to the same 404 envelope so the
    caller cannot distinguish quarantined-but-present from truly-absent.
    """
    session_factory = get_configured_session_factory()

    async with session_factory() as session:
        envelope = await _load_visible_envelope(session, message_id)

    if envelope is None:
        raise AMCError(
            code=ErrorCode.MESSAGE_NOT_FOUND,
            message=f"Message {message_id!r} not found.",
        )
    return envelope


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_visible_envelope(
    session: AsyncSession,
    message_id: str,
) -> Envelope | None:
    """Reconstruct the :class:`Envelope` for ``message_id`` if it is visible.

    Returns ``None`` when the row is missing OR ``allowlist_status='unknown'``.
    The caller turns that into the standard ``404 MESSAGE_NOT_FOUND`` envelope.

    Mirrors the hydration path used by the webhook worker (joining ``senders``
    for ``display_name`` / ``person_id``, decoding ``attachments_json`` and
    ``raw_json``) so the wire shape is identical.
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT m.id, m.source, m.channel_id, m.channel_type, "
                    "       m.sender_id, m.text, m.reply_to, m.direction, "
                    "       m.allowlist_status, m.message_ts, "
                    "       m.attachments_json, m.raw_json, "
                    "       s.display_name AS sender_display_name, "
                    "       s.person_id AS sender_person_id "
                    "FROM messages m "
                    "LEFT JOIN senders s "
                    "  ON s.source = m.source AND s.sender_id = m.sender_id "
                    "WHERE m.id = :id"
                ),
                {"id": message_id},
            )
        )
        .mappings()
        .first()
    )

    if row is None:
        return None

    if row["allowlist_status"] == "unknown":
        # Quarantined inbound — treat as not-visible per §7.4.2.
        return None

    attachments_json = row.get("attachments_json")
    attachments: list[dict[str, Any]]
    if attachments_json:
        try:
            decoded = json.loads(attachments_json)
        except json.JSONDecodeError:
            decoded = []
        attachments = decoded if isinstance(decoded, list) else []
    else:
        attachments = []

    raw_json = row.get("raw_json")
    raw: dict[str, Any]
    if raw_json:
        try:
            decoded_raw = json.loads(raw_json)
        except json.JSONDecodeError:
            decoded_raw = {}
        raw = decoded_raw if isinstance(decoded_raw, dict) else {}
    else:
        raw = {}

    envelope_dict = {
        "id": row["id"],
        "source": row["source"],
        "channel_id": row["channel_id"],
        "channel_type": row["channel_type"],
        "sender": {
            "id": row["sender_id"],
            "display_name": row["sender_display_name"] or row["sender_id"],
            "person_id": row["sender_person_id"],
        },
        "text": row["text"],
        "attachments": attachments,
        "reply_to": row["reply_to"],
        "timestamp": row["message_ts"],
        "direction": row["direction"],
        "raw": raw,
    }
    return Envelope.model_validate(envelope_dict)
