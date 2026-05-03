"""Read-only async reader for macOS ``~/Library/Messages/chat.db``.

This module owns the **inbound** half of the iMessage path (spec §5.1
REQ-AMC-001 + §7.5). It opens the chat.db SQLite file in *read-only*
mode and exposes a polling cursor that returns rows newer than a
caller-supplied ``last_seen_rowid`` — the caller is responsible for
persisting that cursor (lives in ``connector_state`` per spec §5.1).

Design notes:

* **Read-only is enforced two ways**: ``sqlite3.connect("file:{path}?mode=ro", uri=True)``
  AND ``PRAGMA query_only = ON`` immediately after open. URI mode-ro
  alone is insufficient because SQLite still attempts journal recovery
  on open; ``query_only`` rejects any later attempt to write.
* **Never blocks the event loop.** Every SQLite call is dispatched via
  ``asyncio.to_thread``. The synchronous ``sqlite3`` connection lives
  on a dedicated worker thread and is never touched from the loop
  thread directly. This matches §5.1 Technical Notes ("never blocks
  the event loop") and §7.7 (SQLite single-writer / short transactions).
* **Cursor is the caller's responsibility.** The reader is stateless
  with respect to ``last_seen_rowid``; concurrent calls cannot corrupt
  a cursor that doesn't live here. The acceptance criterion "concurrent
  calls don't corrupt the cursor" is satisfied by *not having* one.
* **DM-only filter.** v1 surfaces only ``channel_type='dm'``. Group
  chats (``chat.style != 45``) are filtered out at the query level so
  later wiring layers never see them.
* **Outbound rows (``is_from_me=1``) ARE surfaced** but tagged so the
  wiring layer can decide whether to forward them. Per spec §5.2 the
  adapter persists outbound rows for context-replay, but the wiring
  layer (task #59) decides the policy.

The shape returned by :meth:`ChatDbReader.fetch_new` is a list of
plain ``dict`` rows. Each row carries:

* ``rowid`` — the chat.db ``message.ROWID`` (monotonic; cursor key)
* ``chat_guid`` — the chat.db ``chat.guid`` (e.g. ``iMessage;-;+15551234567``);
  used by AppleScript send routing
* ``sender_handle`` — E.164 phone (``+15551234567``) or Apple-ID email
* ``sender_kind`` — ``"phone"`` or ``"email"``
* ``text`` — the resolved message text (``message.text`` if present,
  else decoded ``attributedBody``, else ``None``)
* ``attributed_body`` — raw typedstream bytes when ``message.text`` is NULL
  (preserved for forensics); ``None`` when ``message.text`` was present
* ``attachments`` — list of ``{path, mime}`` dicts (empty list if none)
* ``timestamp`` — timezone-aware UTC ``datetime`` derived from mach time
* ``is_from_me`` — bool; True for the operator's own outbound messages
* ``raw_json`` — JSON-serializable dict mirroring the raw chat.db row
  (per spec §7.3.3 ``raw_json``); blobs are surfaced as hex strings so
  the dict round-trips through ``json.dumps``.

Errors:

* :class:`ChatDbPathMissingError` — raised at construction if the chat.db
  path doesn't exist. Callers should treat this at startup as the
  ``connectors.imessage.state="degraded"`` health signal (§5.1 error
  handling).
* :class:`sqlite3.OperationalError` with ``"database is locked"`` — the
  reader logs the event and re-raises so the polling loop can sleep
  and retry on the next cycle. Real chat.db is occasionally locked by
  Messages.app's own writers; this is a transient condition.

Phone normalization:

The macOS handle table stores phone numbers already in E.164
(``+15551234567``) most of the time, but field cases occur where digits
arrive raw (``5551234567``) or with formatting (``(555) 123-4567``).
This module normalizes by stripping non-digits and prepending ``+1`` for
10-digit US numbers. **Assumption**: country defaults to ``+1`` (US/CA);
non-US numbers must already arrive with a leading ``+``. The stdlib
approach avoids adding the ``phonenumbers`` dependency for v1; spec §13
R-3 captures this as an acceptable limitation.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "MACH_EPOCH",
    "ChatDbPathMissingError",
    "ChatDbReader",
    "decode_attributed_body",
    "normalize_handle",
]

_logger = logging.getLogger("amc.connectors.imessage.reader")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Apple's Mach reference epoch — nanoseconds since 2001-01-01 UTC.
MACH_EPOCH: datetime = datetime(2001, 1, 1, tzinfo=UTC)

# Apple typedstream archive magic for `attributedBody`. Per phase 0 findings
# §1.3, the on-disk form is the *typedstream* archive (NOT NSKeyedArchiver /
# bplist).
_TYPEDSTREAM_MAGIC: bytes = b"\x04\x0bstreamtyped"

# macOS chat.style for direct-message threads. Group threads use other
# style codes (43 for SMS group, etc.); v1 surfaces only DMs.
_DM_CHAT_STYLE: int = 45

# Polling query. Selects every column from `message` plus joined fields
# from `handle` and `chat`. Attachments are loaded via a second query
# per row (kept separate so the row-set stays small for the common
# no-attachment case). The `cmj.message_date` join field exists per
# fixture schema but we sort by `m.ROWID` because that's the spec's
# cursor key (§5.1 Acceptance Criteria: "ASC by ROWID").
_POLL_QUERY: str = """
    SELECT
        m.ROWID                AS rowid,
        m.guid                 AS message_guid,
        m.text                 AS text,
        m.attributedBody       AS attributed_body,
        m.handle_id            AS handle_rowid,
        m.date                 AS date_ns,
        m.is_from_me           AS is_from_me,
        m.cache_has_attachments AS has_attachments,
        m.service              AS service,
        h.id                   AS handle_id_str,
        h.service              AS handle_service,
        c.guid                 AS chat_guid,
        c.style                AS chat_style
    FROM message m
    JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    JOIN chat c                ON c.ROWID = cmj.chat_id
    LEFT JOIN handle h         ON h.ROWID = m.handle_id
    WHERE m.ROWID > :last_seen
      AND c.style = :dm_style
    ORDER BY m.ROWID ASC
"""

_ATTACHMENT_QUERY: str = """
    SELECT
        a.ROWID     AS rowid,
        a.guid      AS guid,
        a.filename  AS filename,
        a.mime_type AS mime_type,
        a.uti       AS uti,
        a.total_bytes AS total_bytes
    FROM attachment a
    JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
    WHERE maj.message_id = :message_id
    ORDER BY a.ROWID ASC
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChatDbPathMissingError(FileNotFoundError):
    """Raised at construction time when the chat.db path doesn't exist.

    The connector should map this to ``connectors.imessage.state="degraded"``
    in ``/healthz`` (spec §5.1 error handling).
    """


# ---------------------------------------------------------------------------
# attributedBody decoder
# ---------------------------------------------------------------------------


def decode_attributed_body(blob: bytes) -> str | None:
    """Decode an Apple ``attributedBody`` typedstream archive to text.

    The on-disk layout (per phase 0 findings §1.3) is::

        \\x04\\x0Bstreamtyped + version trailer + class hierarchy
        (NSAttributedString -> NSObject + NSString class) +
        length-prefixed UTF-8 string + attribute-run trailer.

    Strategy: find the ``NSString`` class declaration; the message
    body's UTF-8 bytes lie between the class-version trailer (a small
    fixed prefix written by the typedstream encoder) and the
    attribute-run sentinel ``\\x86``. We bracket the payload by:

    1. Anchoring on the ``NSString`` class declaration.
    2. Locating the next ``\\x86`` byte (start of attribute-run dict).
    3. Walking back from ``\\x86`` to find a single-byte length prefix
       whose value matches the distance from "byte after the length"
       up to the ``\\x86`` sentinel.
    4. Falling back to multi-byte length forms (``0x81`` 16-bit,
       ``0x82`` 32-bit) when the single-byte form doesn't fit.

    Returns ``None`` when the blob doesn't carry the typedstream magic
    or no plausible string body can be located. The connector treats
    that as "no decodable text" and falls back to ``message.text`` or
    leaves the message text as ``None`` for the wiring layer.
    """
    if not blob or not blob.startswith(_TYPEDSTREAM_MAGIC):
        return None

    # Find the NSString (or NSMutableString) class declaration. The
    # string body always appears AFTER the class declaration.
    anchor = blob.find(b"NSString")
    if anchor < 0:
        anchor = blob.find(b"NSMutableString")
        if anchor < 0:
            return None
        anchor_end = anchor + len(b"NSMutableString")
    else:
        anchor_end = anchor + len(b"NSString")

    # Find the attribute-run sentinel that closes the string payload.
    # In real archives this is the next 0x86 byte after the class
    # declaration. If we can't find it, fall back to scanning to EOF.
    sentinel = blob.find(b"\x86", anchor_end)
    payload_end_limit = sentinel if sentinel > 0 else len(blob)

    # Scan forward from `anchor_end` for a length prefix that brackets
    # a UTF-8 string ending exactly at `payload_end_limit`. The class
    # declaration is followed by a small fixed trailer (object-table
    # delta + type code + version byte); the length byte appears within
    # ~16 bytes of `anchor_end`.
    for offset in range(anchor_end, min(anchor_end + 32, payload_end_limit)):
        length_byte = blob[offset]
        # Single-byte length form: 0 < n < 0x81 (0x81/0x82/0xFF reserved).
        if 0 < length_byte < 0x81:
            payload_start = offset + 1
            payload_end = payload_start + length_byte
            if payload_end > payload_end_limit:
                continue
            # Strict end-anchored match: the payload must end exactly
            # at the attribute-run sentinel for short strings.
            if sentinel > 0 and payload_end != payload_end_limit:
                continue
            text = _try_decode_utf8(blob[payload_start:payload_end])
            if text is not None and _looks_like_message_text(text):
                return text
        elif length_byte == 0x81:
            # 16-bit length follows (little-endian per typedstream).
            if offset + 3 > len(blob):
                continue
            length = int.from_bytes(blob[offset + 1 : offset + 3], "little")
            payload_start = offset + 3
            payload_end = payload_start + length
            if payload_end > payload_end_limit:
                continue
            if sentinel > 0 and payload_end != payload_end_limit:
                continue
            text = _try_decode_utf8(blob[payload_start:payload_end])
            if text is not None and _looks_like_message_text(text):
                return text
        elif length_byte == 0x82:
            # 32-bit length follows.
            if offset + 5 > len(blob):
                continue
            length = int.from_bytes(blob[offset + 1 : offset + 5], "little")
            payload_start = offset + 5
            payload_end = payload_start + length
            if payload_end > payload_end_limit:
                continue
            if sentinel > 0 and payload_end != payload_end_limit:
                continue
            text = _try_decode_utf8(blob[payload_start:payload_end])
            if text is not None and _looks_like_message_text(text):
                return text

    return None


def _try_decode_utf8(data: bytes) -> str | None:
    """Decode ``data`` as UTF-8 returning ``None`` on failure."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _looks_like_message_text(text: str) -> bool:
    """Heuristic: real message text contains only printable / whitespace.

    Rejects strings that are purely ASCII control chars or contain the
    embedded class names that bracket the real payload.
    """
    if not text:
        return False
    if any(name in text for name in ("NSDictionary", "NSNumber", "NSValue")):
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
    return printable == len(text)


# ---------------------------------------------------------------------------
# Handle normalization
# ---------------------------------------------------------------------------


def normalize_handle(handle: str | None) -> tuple[str | None, str | None]:
    """Normalize a chat.db ``handle.id`` value.

    Returns a ``(normalized_handle, kind)`` pair where ``kind`` is one
    of ``"phone"``, ``"email"``, or ``None`` when the input is empty.
    Phone numbers are coerced to E.164 (``+15551234567``); 10-digit US
    numbers get the ``+1`` prefix prepended. Email Apple-IDs are
    preserved as-is.

    Assumption (documented in module docstring): country defaults to US
    (``+1``) for 10-digit raw numbers; all other numeric inputs that
    don't start with ``+`` get a bare ``+`` prepended (best-effort).
    """
    if not handle:
        return None, None

    # Email Apple-ID: contains '@', preserve as-is (lowercased to match
    # how the chat.db typically stores them).
    if "@" in handle:
        return handle, "email"

    # Phone normalization: strip everything that isn't a digit, then
    # rebuild in E.164.
    digits = "".join(ch for ch in handle if ch.isdigit())
    if not digits:
        # Some other non-empty, non-email, non-numeric string — pass
        # through unchanged so the caller can see the raw value.
        return handle, None

    if handle.startswith("+"):
        # Already E.164-shaped; return as-is with normalized digits.
        return f"+{digits}", "phone"

    if len(digits) == 10:
        # 10-digit US number.
        return f"+1{digits}", "phone"

    if len(digits) == 11 and digits.startswith("1"):
        # Already includes country code without the '+'.
        return f"+{digits}", "phone"

    # Best-effort fallback: prepend '+' so downstream code at least
    # recognizes the shape.
    return f"+{digits}", "phone"


# ---------------------------------------------------------------------------
# Time conversion
# ---------------------------------------------------------------------------


def _mach_ns_to_datetime(date_ns: int | None) -> datetime:
    """Convert a chat.db ``message.date`` value to a UTC datetime.

    ``message.date`` is mach absolute time: nanoseconds since
    2001-01-01 UTC. ``None`` (rare) maps to the Mach epoch itself; the
    wiring layer can decide whether to drop or normalize such rows.
    """
    if date_ns is None:
        return MACH_EPOCH
    return MACH_EPOCH + timedelta(seconds=date_ns / 1_000_000_000)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class ChatDbReader:
    """Async, read-only reader over a macOS chat.db SQLite file.

    The connection is opened lazily on the first call to
    :meth:`fetch_new` and cached for the lifetime of the reader.
    Calling :meth:`close` releases the underlying connection.

    Concurrency: the reader serializes SQLite work behind a single
    asyncio lock, so even concurrent calls don't corrupt connection
    state. The cursor (``last_seen``) is the caller's responsibility.

    Args:
        path: Path to the chat.db file. Tests pass a temp copy of the
            Phase 0 fixture; production passes
            ``~/Library/Messages/chat.db``.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise ChatDbPathMissingError(
                f"chat.db not found at {self._path}. "
                "Adapter health should report imessage connector as 'degraded'."
            )
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Absolute path to the chat.db this reader reads."""
        return self._path

    async def fetch_new(self, last_seen_rowid: int) -> list[dict[str, Any]]:
        """Return all messages with ``ROWID > last_seen_rowid``.

        Rows are returned in ascending ROWID order so the caller can
        advance its cursor to the last returned ``rowid`` after
        successful processing. Group-chat rows are filtered out at the
        SQL layer (DM only); ``is_from_me=1`` rows are returned with
        the ``is_from_me`` flag set so the wiring layer can decide.

        Re-raises ``sqlite3.OperationalError`` with ``"database is locked"``
        after logging — the caller's polling loop should sleep and
        retry on the next cycle (spec §5.1 error handling).
        """
        async with self._lock:
            return await asyncio.to_thread(self._fetch_new_sync, last_seen_rowid)

    def _fetch_new_sync(self, last_seen_rowid: int) -> list[dict[str, Any]]:
        conn = self._ensure_conn()
        try:
            rows = conn.execute(
                _POLL_QUERY,
                {"last_seen": last_seen_rowid, "dm_style": _DM_CHAT_STYLE},
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # Real chat.db is occasionally locked by Messages.app's
            # internal writers; let the caller decide to retry on the
            # next poll cycle (per acceptance criteria).
            if "locked" in str(exc).lower():
                _logger.warning(
                    "chat_db_locked",
                    extra={"path": str(self._path), "error": str(exc)},
                )
            raise

        result: list[dict[str, Any]] = []
        for row in rows:
            attachments: list[dict[str, str | None]] = []
            if row["has_attachments"]:
                attachment_rows = conn.execute(
                    _ATTACHMENT_QUERY,
                    {"message_id": row["rowid"]},
                ).fetchall()
                for arow in attachment_rows:
                    attachments.append(
                        {
                            "path": arow["filename"],
                            "mime": arow["mime_type"],
                        }
                    )

            result.append(_build_message_dict(row, attachments))
        return result

    async def close(self) -> None:
        """Release the underlying sqlite3 connection.

        Idempotent. Safe to call multiple times.
        """
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    async def __aenter__(self) -> ChatDbReader:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            uri = f"file:{self._path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            # Belt-and-suspenders: URI mode=ro alone allows journal
            # recovery; query_only rejects every later attempt to write.
            conn.execute("PRAGMA query_only = ON")
            conn.row_factory = sqlite3.Row
            self._conn = conn
        return self._conn


# ---------------------------------------------------------------------------
# Row → dict helper
# ---------------------------------------------------------------------------


def _build_message_dict(
    row: sqlite3.Row,
    attachments: list[dict[str, str | None]],
) -> dict[str, Any]:
    """Assemble the public dict shape for one chat.db message row."""
    raw_text: str | None = row["text"]
    attributed_blob: bytes | None = row["attributed_body"]

    if raw_text is None and attributed_blob is not None:
        decoded = decode_attributed_body(attributed_blob)
        text: str | None = decoded
    else:
        text = raw_text

    sender_handle, sender_kind = normalize_handle(row["handle_id_str"])
    timestamp = _mach_ns_to_datetime(row["date_ns"])
    is_from_me = bool(row["is_from_me"])

    return {
        "rowid": int(row["rowid"]),
        "chat_guid": row["chat_guid"],
        "sender_handle": sender_handle,
        "sender_kind": sender_kind,
        "text": text,
        "attributed_body": attributed_blob,
        "attachments": attachments,
        "timestamp": timestamp,
        "is_from_me": is_from_me,
        "raw_json": _row_to_raw_json(row, attachments),
    }


def _row_to_raw_json(
    row: sqlite3.Row,
    attachments: list[dict[str, str | None]],
) -> dict[str, Any]:
    """Convert a sqlite3 Row + attachments to a JSON-serializable dict.

    Blobs are encoded as hex strings so the result round-trips through
    ``json.dumps``. This is the payload the wiring layer persists to
    ``messages.raw_json`` (spec §7.3.3) for forensics.
    """
    raw: dict[str, Any] = {}
    # `sqlite3.Row.keys()` is the only way to enumerate column names —
    # iterating the Row itself yields values, not keys. SIM118 misfires.
    for key in row.keys():  # noqa: SIM118
        value: Any = row[key]
        if isinstance(value, bytes):
            value = value.hex()
        raw[key] = value
    raw["attachments"] = list(attachments)
    return raw
