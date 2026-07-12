"""Local attachment store for inbound re-hosting (spec §5.8 / §7.3.3).

When a connector ingests an inbound message that carries one or more
attachments, the platform-native bytes (a Discord CDN URL or an iMessage
local file path) become unstable references — Discord URLs expire and
iMessage paths are inside the user's Messages cache. The adapter solves
this by **re-hosting** every inbound attachment under a stable
adapter-served URL backed by a local copy of the bytes.

This module owns the local copy step. The HTTP serving side (``GET
/attachments/{id}``) and the daily retention sweeper are separate
concerns handled elsewhere.

Public surface
--------------
* :class:`AttachmentRow` — the value returned by :meth:`AttachmentStore.rehost`,
  shaped to match the columns of the ``attachments`` table (spec §7.3.3).
  The connector stitches this into the normalized envelope before handing
  off to :class:`amg.core.message_sink.MessageSink`.
* :class:`AttachmentStoreError` — raised at startup when the configured
  store directory cannot be created or written to (refuse-to-start).
* :class:`AttachmentStore` — the copier. Construct once at adapter
  startup with a directory and a bind URL; share across connectors.

Behavior summary (spec §5.8 acceptance + edge cases)
----------------------------------------------------
On :meth:`AttachmentStore.rehost`:

* Mints an ``att_<ULID>`` id (Crockford base32, 26 chars).
* Copies ``source_path`` → ``<store_dir>/<id>``. Copy is the literal byte
  stream, no transcoding.
* Detects ``mime`` via stdlib :mod:`mimetypes` if the caller did not
  provide one; falls back to ``application/octet-stream``.
* Computes ``size_bytes`` from the on-disk size of the copy.
* Returns an :class:`AttachmentRow` that the connector packs into the
  envelope (with ``url = bind_url(id)``) and into the
  ``attachments`` row (with ``bytes_path = absolute_local_path``).

On error (source missing, disk full, copy fails for any reason):

* Logs a ``WARN`` event via structlog.
* Returns ``None``. The envelope ships **without** that attachment;
  the message itself still flows.
* No partial file remains under the store dir (cleanup is best-effort
  via ``unlink(missing_ok=True)``).

Concurrency
-----------
File I/O is dispatched through :func:`asyncio.to_thread` so the event
loop is never blocked, matching the rest of the adapter. There is no
shared in-memory state — each :meth:`rehost` is independent.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog
from ulid import ULID

__all__ = [
    "DEFAULT_BIND_URL",
    "DEFAULT_STORE_DIR",
    "ENV_ATTACHMENT_DIR",
    "FALLBACK_MIME",
    "AttachmentRow",
    "AttachmentStore",
    "AttachmentStoreError",
]

_log = structlog.get_logger("amg.attachments")

# Env-var name; mirrored in `.env.example` and spec §11.2.
ENV_ATTACHMENT_DIR: Final[str] = "AMG_ATTACHMENT_DIR"

# Default store dir per spec §5.8 / §11.2. Resolved at use time so tests can
# monkeypatch ``$HOME`` and pick up the override without re-importing.
DEFAULT_STORE_DIR: Final[str] = "~/Library/Application Support/messaging-agent/attachments"

# Default bind URL the adapter serves attachments from (spec §5.8 acceptance).
# Connectors that pass their own bind URL override this; the default matches
# the spec §11.2 ``AMG_BIND_HOST=127.0.0.1`` / ``AMG_BIND_PORT=8080`` defaults.
DEFAULT_BIND_URL: Final[str] = "http://127.0.0.1:8080"

# Used when neither the caller nor :mod:`mimetypes` can identify the file.
FALLBACK_MIME: Final[str] = "application/octet-stream"


class AttachmentStoreError(RuntimeError):
    """Raised at startup when the configured store dir is unusable.

    The adapter refuses to start in this case (per spec §5.8 / §6.4
    "refuse-to-start with clear error" semantics) so the operator
    notices immediately rather than discovering a silent inbound-loss
    in production.
    """


@dataclass(frozen=True, slots=True)
class AttachmentRow:
    """Re-hosted attachment metadata (spec §7.3.3 ``attachments`` columns).

    The shape mirrors the on-disk row: the connector hands this dict
    to the envelope builder (which uses ``id`` + ``mime`` + ``size_bytes``
    + the adapter-hosted URL) and to the persistence layer (which
    uses ``id`` + ``mime`` + ``size_bytes`` + ``bytes_path`` +
    ``original_url_or_path``).
    """

    id: str
    mime: str
    size_bytes: int
    bytes_path: str
    original_url_or_path: str


class AttachmentStore:
    """Local attachment re-host store.

    Construct once at adapter startup; share the instance across
    connectors. The constructor validates that the store directory
    exists (or can be created) and is writable, raising
    :class:`AttachmentStoreError` otherwise.
    """

    __slots__ = ("_dir", "_bind_url")

    def __init__(
        self,
        store_dir: str | os.PathLike[str] | None = None,
        *,
        bind_url: str = DEFAULT_BIND_URL,
    ) -> None:
        """Resolve, create, and validate the store directory.

        Args:
            store_dir: Override the directory. Falls back to
                ``$AMG_ATTACHMENT_DIR`` then :data:`DEFAULT_STORE_DIR`.
                A leading ``~`` is expanded.
            bind_url: Base URL the adapter serves attachments from
                (e.g. ``http://127.0.0.1:8080``). Trailing slash is
                stripped. The full per-attachment URL is
                ``f"{bind_url}/attachments/{id}"`` (spec §5.8).

        Raises:
            AttachmentStoreError: The directory does not exist and
                cannot be created (parent unwritable, permission
                denied, etc.) or the directory exists but the process
                cannot write to it.
        """

        resolved = _resolve_store_dir(store_dir)
        _ensure_writable(resolved)
        self._dir: Path = resolved
        # Strip any trailing slash so concatenation produces exactly one.
        self._bind_url: str = bind_url.rstrip("/")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def store_dir(self) -> Path:
        """Absolute path to the store directory (read-only)."""
        return self._dir

    @property
    def bind_url(self) -> str:
        """Adapter bind URL without trailing slash (read-only)."""
        return self._bind_url

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def url_for(self, attachment_id: str) -> str:
        """Return the canonical adapter-hosted URL for ``attachment_id``."""
        return f"{self._bind_url}/attachments/{attachment_id}"

    async def rehost(
        self,
        source_path: str | os.PathLike[str],
        mime: str | None,
    ) -> AttachmentRow | None:
        """Copy ``source_path`` into the store and return its row.

        Returns ``None`` (and logs a WARN) on any recoverable failure:
        source missing, disk full, permission denied on copy, etc.
        The caller (a connector) is expected to ship the envelope
        anyway, sans this attachment, per spec §5.8 edge cases.

        Args:
            source_path: Local filesystem path to the inbound file
                (an iMessage attachment path or a previously-fetched
                Discord temp file). Must be readable by this process.
            mime: Caller-supplied MIME, or ``None`` to detect via
                :func:`mimetypes.guess_type`. If detection fails the
                row gets :data:`FALLBACK_MIME`.

        Returns:
            An :class:`AttachmentRow` on success, ``None`` on a
            handled error (already logged).
        """

        attachment_id = f"att_{ULID()}"
        dest = self._dir / attachment_id
        # Preserve the source path string for the row even if it's a
        # PathLike object — the schema column is ``original_url_or_path``
        # and the spec keeps it for forensics (§7.3.3).
        original = os.fspath(source_path)

        try:
            size_bytes = await asyncio.to_thread(
                _copy_file,
                original,
                dest,
            )
        except FileNotFoundError:
            _log.warning(
                "attachment_source_missing",
                source_path=original,
                attachment_id=attachment_id,
            )
            return None
        except PermissionError as exc:
            _log.warning(
                "attachment_source_unreadable",
                source_path=original,
                attachment_id=attachment_id,
                error=str(exc),
            )
            return None
        except OSError as exc:
            # Disk full surfaces as ENOSPC; treat any disk-side OSError
            # the same way (EDQUOT, EROFS, EIO). Best-effort cleanup
            # of any partial file the failed copy left behind.
            _log.warning(
                "attachment_copy_failed",
                source_path=original,
                attachment_id=attachment_id,
                errno=exc.errno,
                errno_name=errno.errorcode.get(exc.errno or 0, "UNKNOWN"),
                error=str(exc),
            )
            await asyncio.to_thread(_unlink_quiet, dest)
            return None

        resolved_mime = _resolve_mime(mime, original)

        _log.info(
            "attachment_rehosted",
            attachment_id=attachment_id,
            source_path=original,
            mime=resolved_mime,
            size_bytes=size_bytes,
        )
        return AttachmentRow(
            id=attachment_id,
            mime=resolved_mime,
            size_bytes=size_bytes,
            bytes_path=str(dest),
            original_url_or_path=original,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_store_dir(override: str | os.PathLike[str] | None) -> Path:
    """Pick the store dir from override → env → default; expand ``~``."""
    if override is not None:
        candidate = os.fspath(override)
    else:
        candidate = os.environ.get(ENV_ATTACHMENT_DIR) or DEFAULT_STORE_DIR
    return Path(candidate).expanduser()


def _ensure_writable(path: Path) -> None:
    """Create ``path`` if missing and verify it is writable.

    Raises :class:`AttachmentStoreError` with a clear message on any
    permission / OS failure so the adapter refuses to start.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise AttachmentStoreError(
            f"Cannot create attachment store directory {path!s}: "
            f"permission denied. Set {ENV_ATTACHMENT_DIR} to a writable "
            f"location or grant write permission to the existing path. "
            f"Underlying error: {exc}"
        ) from exc
    except OSError as exc:
        raise AttachmentStoreError(
            f"Cannot create attachment store directory {path!s}: {exc}. "
            f"Set {ENV_ATTACHMENT_DIR} to a writable location."
        ) from exc

    if not os.access(path, os.W_OK):
        raise AttachmentStoreError(
            f"Attachment store directory {path!s} exists but is not writable "
            f"by this process. Fix permissions or set {ENV_ATTACHMENT_DIR} "
            f"to a writable location."
        )


def _copy_file(source: str, dest: Path) -> int:
    """Copy ``source`` → ``dest`` and return the resulting byte size.

    Runs in a worker thread (dispatched via :func:`asyncio.to_thread`).
    Uses :func:`shutil.copyfile` which streams in chunks so even large
    attachments do not exhaust memory. The destination size is read
    back from disk (rather than computed from the source) so the row
    reflects the actual stored bytes.
    """
    shutil.copyfile(source, dest)
    return dest.stat().st_size


def _unlink_quiet(path: Path) -> None:
    """Best-effort unlink; ignore "already gone" and any other OSError.

    Cleanup failure is not interesting — the row was never registered,
    so a stray byte file at most wastes a few KB until the retention
    sweeper runs.
    """
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _resolve_mime(provided: str | None, source_path: str) -> str:
    """Return the caller-supplied mime, else guess by extension, else fallback."""
    if provided:
        return provided
    guessed, _ = mimetypes.guess_type(source_path)
    return guessed or FALLBACK_MIME
