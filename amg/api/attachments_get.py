"""``GET /attachments/{id}`` — serve re-hosted attachment bytes.

Spec references: §5.8 (re-hosted attachments), §7.4.9 (endpoint contract),
§7.4 header table (bearer required, ``X-Agent-ID`` not required).

Behavior
--------
The connector copies inbound platform bytes (Discord CDN URLs, iMessage
local files) into the local store at ``$AMG_ATTACHMENT_DIR/<att_id>`` and
records the absolute path in ``attachments.bytes_path`` along with
``mime`` and ``size_bytes`` (spec §7.3.3). This route streams those bytes
back to the agent under the stable adapter URL the connector embedded in
the envelope (``http://127.0.0.1:8080/attachments/<id>``).

Visibility / 404 cases
----------------------
* Row missing → 404. The id is not (or never was) known to the adapter.
* ``bytes_path IS NULL`` → 404. The retention sweeper has deleted the
  bytes per the 90-day policy (spec §3.6 / §5.8); the row is retained for
  forensics but the bytes are gone.
* ``bytes_path`` set but the file is missing on disk → 404, with an
  ERROR-level log so the operator notices the inconsistency. This
  shouldn't happen under normal operation (sweeper updates the row) so
  we treat it as a corruption signal.

Headers on success
------------------
* ``Content-Type``: ``mime`` from the row.
* ``Content-Length``: ``size_bytes`` from the row (FastAPI's
  ``FileResponse`` also stat()s the file; passing it explicitly via
  ``headers=`` documents intent and lets clients length-check before
  reading).
* ``Content-Disposition: inline`` so browsers/agents render rather than
  download. The spec §7.4.9 leaves this open; ``inline`` matches the
  "view" use case (agent showing an image) while still allowing clients
  that want to save to do so.

Wiring (#41 ``amg/app.py``)
---------------------------
The session factory is installed at startup via
:func:`configure_session_factory`. The route reads it through
:func:`get_configured_session_factory`. Tests configure and reset via
the same hooks rather than monkey-patching ``app.dependency_overrides``,
matching the pattern in :mod:`amg.api.messages_get` and
:mod:`amg.api.messages_mark_read`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from amg.core.auth import require_bearer
from amg.core.envelope import AttachmentId
from amg.core.errors import AMGError, ErrorCode

__all__ = [
    "configure_session_factory",
    "get_configured_session_factory",
    "reset_session_factory",
    "router",
]


_log = structlog.get_logger("amg.api.attachments_get")


# ---------------------------------------------------------------------------
# Session-factory holder
# ---------------------------------------------------------------------------

# Module-level holder so the route can resolve the configured session factory
# without callers having to thread it through every ``Depends``. Set via
# :func:`configure_session_factory` at app startup; reset for tests via
# :func:`reset_session_factory`. Mirrors the pattern in
# :mod:`amg.api.messages_get`.
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

router = APIRouter(prefix="/attachments", tags=["attachments"])


_AttachmentIdPath = Annotated[
    AttachmentId,
    PathParam(
        description="ULID-prefixed attachment id (``att_<26 Crockford-base32 chars>``).",
    ),
]


@router.get(
    "/{attachment_id}",
    dependencies=[Depends(require_bearer)],
    # ``response_class`` is set so the OpenAPI schema documents this as a
    # binary stream rather than the default JSON.
    response_class=FileResponse,
)
async def get_attachment(attachment_id: _AttachmentIdPath) -> FileResponse:
    """Stream the bytes for ``attachment_id`` from the local re-host store.

    Returns 404 (``MESSAGE_NOT_FOUND`` — spec §7.4.12 has no dedicated
    attachment-not-found code; reusing the closest neighbour keeps the
    error envelope shape stable) when the row is missing, the bytes
    have been retention-deleted (``bytes_path IS NULL``), or the file
    on disk is absent.
    """
    session_factory = get_configured_session_factory()

    async with session_factory() as session:
        row = await _load_attachment(session, attachment_id)

    if row is None:
        # Missing row OR bytes_path is NULL (retention sweeper deleted
        # the bytes). Per §7.4.9 both collapse to the same 404 so the
        # agent cannot probe whether a deleted attachment ever existed.
        raise AMGError(
            code=ErrorCode.MESSAGE_NOT_FOUND,
            message=f"Attachment {attachment_id!r} not found.",
        )

    bytes_path: str = row["bytes_path"]
    mime: str = row["mime"]
    size_bytes: int = row["size_bytes"]

    if not os.path.isfile(bytes_path):
        # Row claims bytes are present but the file is gone. This is a
        # corruption signal — log ERROR so the operator notices, then
        # collapse to 404 so the agent sees the same shape as a clean
        # delete.
        _log.error(
            "attachment_bytes_missing_on_disk",
            attachment_id=attachment_id,
            bytes_path=bytes_path,
        )
        raise AMGError(
            code=ErrorCode.MESSAGE_NOT_FOUND,
            message=f"Attachment {attachment_id!r} not found.",
        )

    # Pass ``Content-Length`` from the row rather than relying on the
    # framework to stat() the file. The values should agree but the row
    # is the source of truth (the bytes were copied byte-for-byte from
    # the source per :class:`amg.core.attachments.AttachmentStore`).
    return FileResponse(
        path=Path(bytes_path),
        media_type=mime,
        headers={
            "Content-Length": str(size_bytes),
            "Content-Disposition": "inline",
        },
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_attachment(
    session: AsyncSession,
    attachment_id: str,
) -> dict[str, object] | None:
    """Return the attachment row keyed by id, or ``None``.

    Returns ``None`` when:

    * No row exists with that id.
    * The row exists but ``bytes_path IS NULL`` (retention-deleted).

    Returns a dict with ``mime``, ``size_bytes``, and ``bytes_path`` on
    a hit. Caller still verifies the on-disk file exists before
    streaming.
    """
    result = (
        (
            await session.execute(
                text(
                    "SELECT mime, size_bytes, bytes_path "
                    "FROM attachments "
                    "WHERE id = :id"
                ),
                {"id": attachment_id},
            )
        )
        .mappings()
        .first()
    )

    if result is None:
        return None
    if result["bytes_path"] is None:
        return None
    return dict(result)
