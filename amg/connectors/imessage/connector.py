"""iMessage platform connector wiring (spec §5.1, §5.2, §7.5 iMessage path).

Owns both halves of the iMessage <-> AMG bridge:

* **Inbound**: a background asyncio task polls
  :class:`amg.connectors.imessage.reader.ChatDbReader` every
  ``poll_interval_seconds`` (default 1.0). Each new ``message.ROWID`` row
  is filtered, normalized into an :class:`amg.core.envelope.Envelope`,
  and handed to :class:`amg.core.message_sink.MessageSink`. The sink's
  short transaction persists the row AND advances
  ``connector_state.cursor`` for ``source='imessage'`` atomically (spec
  §5.1 Technical Notes).

* **Outbound**: :meth:`ImessageConnector.send` resolves the destination
  ``chat.guid`` from ``channels.metadata_json`` (set by inbound rows) and
  delegates to the injected
  :class:`amg.connectors.imessage.applescript.AppleScriptSender`.

Channel id format
-----------------
Per spec §7.3.1 ("iMessage uses E.164 phone or Apple ID email"; no
``imessage:`` prefix) the AMG ``channel_id`` for an iMessage DM is the
sender's normalized handle (``+15551234567`` or ``alice@example.com``).
The macOS ``chat.guid`` (e.g. ``iMessage;-;+15551234567``) is the value
AppleScript needs to send to the right thread; we persist it in
``channels.metadata_json`` as ``{"chat_guid": "iMessage;-;+15551234567"}``
on every inbound row, then read it back on outbound.

Cursor format
-------------
``connector_state.cursor`` for ``source='imessage'`` is the *string-encoded*
last-seen ``message.ROWID``. The schema column is ``TEXT NOT NULL``; we
store the integer as decimal (``"42"``) so ``int(cursor)`` is the only
parse step.

Cursor recovery on restart (task #52)
-------------------------------------
``start()`` reads ``connector_state.cursor`` once at boot:

* **Row exists** (warm restart): resume polling from that ROWID. The first
  ``fetch_new`` call uses ``WHERE ROWID > cursor`` so the connector picks
  up exactly where it left off. The cursor was advanced inside the same
  transaction as each previous message INSERT (in :class:`MessageSink`),
  so the persisted cursor never points ahead of the persisted messages.
* **Row missing** (fresh install): seed the cursor to the *current* max
  ``message.ROWID`` in chat.db. This avoids a "historical flood" — on a
  brand new adapter pointed at a long-existing chat.db the operator
  doesn't want every backlogged message replayed as "new". The seed is
  in-memory only; it is persisted to ``connector_state.cursor`` only when
  the next inbound row triggers the sink (transactional advance).
* **Cursor read fails** (AMG DB unreadable / corrupt): log ERROR and fall
  back to the fresh-install path (max ROWID seed). Treating a transient
  AMG DB error as "fresh install" beats degrading the connector — the
  next message INSERT will heal the cursor row.

Stale cursor (cursor row exists but the chat.db has advanced past it
between adapter runs) is handled implicitly by the same code path:
``WHERE ROWID > cursor`` returns every row added in the meantime, in
ASC order. The poller drains them on the first cycle.

Channel-id derivation (spec edge case)
--------------------------------------
For a row from a known sender (``handle.id`` present), the ``channel_id``
is the *normalized handle*. If both ``message.handle_id`` and the chat
have no resolvable sender (rare; the reader's join would have skipped
the row anyway because v1 surfaces only ``style=45`` DMs which always
have a single handle), we drop the row defensively rather than mint a
synthetic id.

is_from_me skip
---------------
Outbound messages we sent are recorded by :meth:`send`; surfacing them
again from the poller would double-count. v1 skips them entirely (the
adapter's ``messages`` table never sees the OS-level outbound copy);
the operator sees only the API-driven outbound row that ``record_outbound``
created. This matches OQ-2 ("outbound never fires webhook") and the
spec §5.2 reuse pattern.

FDA / chat.db missing
---------------------
If the chat.db path is missing (Full Disk Access not granted),
:class:`ChatDbReader` raises
:class:`amg.connectors.imessage.reader.ChatDbPathMissingError` at
*construction time* — long before :meth:`ImessageConnector.start` is
called. The adapter wires the reader inside its lifespan; on that
exception the operator sees the failure at boot and ``/healthz`` can
report ``connectors.imessage.state="degraded"`` (spec §5.1 error
handling row). The connector itself is never instantiated in that path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from ulid import ULID

from amg.connectors.imessage.applescript import AppleScriptSender, SendResult
from amg.connectors.imessage.reader import ChatDbReader
from amg.core.allowlist import AllowlistLoader
from amg.core.attachments import AttachmentRow, AttachmentStore
from amg.core.envelope import (
    Attachment,
    ChannelType,
    Direction,
    Envelope,
    Sender,
    Source,
)

__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "ROWID_GAP_WARN_THRESHOLD",
    "ImessageConnector",
    "MessageSinkLike",
    "SendResult",
]

_logger = logging.getLogger("amg.connectors.imessage.connector")

# Spec §5.1 acceptance criterion: poller picks up new rows within 1s.
DEFAULT_POLL_INTERVAL_SECONDS: float = 1.0

# Task #53: Mac-wake / ROWID-gap detection threshold. When the gap between
# the current cursor and the max ROWID returned by ``fetch_new`` exceeds
# this many rows, emit an observability WARN. The threshold is calibrated
# so continuous traffic at the 1s poll interval (a handful of new rows per
# cycle) never spams the log; only a multi-minute Mac sleep / network blip
# / Messages.app backlog catch-up trips it. The poller still processes
# every gap row in ASC ROWID order — there is no special "catch-up mode".
ROWID_GAP_WARN_THRESHOLD: int = 100


@runtime_checkable
class MessageSinkLike(Protocol):
    """Structural type for the inbound persistence chokepoint.

    The real implementation is :class:`amg.core.message_sink.MessageSink`;
    tests substitute a recorder that satisfies this shape. Using a
    :class:`Protocol` (rather than the concrete class) keeps the connector
    decoupled and lets tests inject a sink without standing up a SQLite
    schema.
    """

    async def record_inbound(self, envelope: Envelope, source: str) -> str:
        """Persist ``envelope`` and advance ``connector_state.cursor`` to ``source``."""
        ...


class ImessageConnector:
    """Inbound poller + outbound AppleScript driver for iMessage.

    Construct once at adapter startup and keep alive for the process
    lifetime. :meth:`start` launches the background poll loop;
    :meth:`stop` cancels it cleanly.

    Args:
        reader: A :class:`ChatDbReader` opened against the chat.db path.
            For production this is ``~/Library/Messages/chat.db``; tests
            pass a writable copy of the Phase 0 fixture.
        sender: An :class:`AppleScriptSender` (real ``osascript`` shim or
            :class:`tests.fakes.applescript.FakeAppleScriptSender`).
        sink: A :class:`MessageSink` (or :class:`MessageSinkLike` test
            double). The connector hands every inbound envelope here;
            cursor advancement is the sink's responsibility.
        allowlist: Loaded :class:`AllowlistLoader`. Used here to override
            the platform display name when the entry sets one. The sink
            re-resolves for the canonical ``allowlist_status`` snapshot,
            so passing the same loader to both is correct (and cheap).
        session_factory: Async SQLAlchemy session factory bound to the
            adapter's SQLite engine. Used to (a) read the cursor at
            startup and (b) UPSERT ``channels.metadata_json`` with the
            chat.guid before delegating to the sink. ``None`` is allowed
            for tests that pass a fake sink and don't need persistence
            side effects beyond what the sink fakes.
        attachment_store: A :class:`AttachmentStore` instance. ``None``
            disables attachment re-hosting (the envelope ships sans
            attachments). v1 always supplies one in production.
        poll_interval_seconds: Sleep between poll cycles. Defaults to
            1.0s per spec §5.1 acceptance criterion.

    Notes on threading:
        Every external call (sink, allowlist resolve, attachment copy)
        is async-safe. The poller is a single asyncio task; concurrent
        :meth:`send` invocations from the HTTP layer are independent
        and don't share state with the poller beyond the AppleScript
        sender (which is documented as concurrency-safe).
    """

    __slots__ = (
        "_reader",
        "_sender",
        "_sink",
        "_allowlist",
        "_session_factory",
        "_attachment_store",
        "_poll_interval",
        "_task",
        "_stop_event",
        "degraded",
    )

    def __init__(
        self,
        *,
        reader: ChatDbReader,
        sender: AppleScriptSender,
        sink: MessageSinkLike,
        allowlist: AllowlistLoader,
        session_factory: async_sessionmaker[Any] | None = None,
        attachment_store: AttachmentStore | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._reader = reader
        self._sender = sender
        self._sink = sink
        self._allowlist = allowlist
        self._session_factory = session_factory
        self._attachment_store = attachment_store
        self._poll_interval = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        # Public marker the adapter reads when chat.db is missing or
        # unreadable (spec §5.1 error handling).
        self.degraded: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the background polling task with cursor recovery.

        Cursor recovery (task #52, spec §5.1):

        * **Warm restart**: ``connector_state.cursor`` row exists for
          ``source='imessage'`` -> resume polling from that ROWID. The
          first :meth:`ChatDbReader.fetch_new` call uses
          ``WHERE ROWID > cursor`` so we never re-emit a row whose
          message INSERT already committed.
        * **Fresh install**: no cursor row -> seed the in-memory cursor
          to the current ``MAX(message.ROWID)`` in chat.db so the first
          poll cycle does not emit the entire backlog as "new". The
          seeded value is not persisted until the next inbound row's
          sink transaction.
        * **Cursor read failure**: log ERROR and fall back to the
          fresh-install path (seed to ``MAX(message.ROWID)``). This
          favors keeping the connector live over flagging it degraded
          on a transient AMG DB hiccup; the next successful sink
          transaction heals the cursor row.

        Idempotent: a second call while a task is already running is a
        no-op.
        """
        if self._task is not None and not self._task.done():
            return

        initial_cursor = await self._resolve_initial_cursor()

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._poll_loop(initial_cursor),
            name="amg.imessage.poller",
        )

    async def _resolve_initial_cursor(self) -> int:
        """Decide the starting cursor for the poll loop.

        Returns the ROWID the *next* poll's ``WHERE ROWID > cursor``
        clause should compare against:

        * ``connector_state`` row present -> stored ROWID (warm restart).
        * ``connector_state`` row missing -> ``MAX(message.ROWID)`` from
          chat.db (fresh install: avoid historical flood).
        * Cursor read raises -> log ERROR, fall back to fresh install.
        """
        try:
            stored = await self._read_cursor_at_startup()
        except (sqlite3.OperationalError, OSError) as exc:
            _logger.error(
                "imessage_cursor_read_failed_treating_as_fresh_install",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            return self._seed_cursor_from_chat_db()

        if stored is not None:
            return stored

        # No row -> fresh install: seed past every existing chat.db row
        # so the operator does not get a historical replay of the entire
        # backlog on first boot.
        return self._seed_cursor_from_chat_db()

    async def stop(self) -> None:
        """Stop the poller and release the reader.

        Idempotent. Safe to call from a shutdown signal handler or
        finalizer. Waits for the in-flight cycle to finish so a
        partially-processed row either commits via the sink (cursor
        advances) or rolls back (cursor stays put).
        """
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._reader.close()

    # ------------------------------------------------------------------
    # Inbound: poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self, initial_cursor: int) -> None:
        """Poll the reader every ``poll_interval`` seconds.

        On any unexpected exception in the inner cycle, log and sleep
        before retrying. Never re-raises so the adapter's lifespan task
        doesn't bubble up errors during normal degraded operation.
        """
        cursor = initial_cursor
        assert self._stop_event is not None  # set by start()
        stop_event = self._stop_event

        while not stop_event.is_set():
            try:
                cursor = await self._poll_once(cursor)
            except Exception:  # noqa: BLE001
                _logger.exception("imessage_poll_cycle_failed")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
                # If the wait returned without timeout, stop was set.
                break
            except TimeoutError:
                # Normal path: poll interval elapsed, loop again.
                continue

    async def _poll_once(self, cursor: int) -> int:
        """One poll cycle. Returns the new cursor (== last advanced rowid).

        Per spec §5.1 acceptance criteria:
        * Rows are fetched in ASC ROWID order.
        * ``is_from_me=1`` is skipped.
        * Group chats are skipped (already filtered at SQL layer).
        * If the sink raises for a row, the cursor does NOT advance for
          that row (we re-raise into the caller, which logs and the next
          cycle will retry the same row).

        Mac-wake / ROWID-gap observability (task #53):
        When the gap between ``cursor`` and the max ROWID returned exceeds
        :data:`ROWID_GAP_WARN_THRESHOLD`, emit a WARN with the cursor and
        new max ROWID. This surfaces sleep-induced backlogs (Mac woke up
        after ``caffeinate`` failed, network blip, Messages.app froze)
        without changing processing semantics — every gap row is still
        processed in ASC ROWID order through the standard loop below.
        Continuous traffic produces tiny gaps (~rows-per-cycle) and never
        trips the threshold, so the WARN does not spam steady-state logs.
        """
        try:
            rows = await self._reader.fetch_new(cursor)
        except sqlite3.OperationalError as exc:
            # chat.db is occasionally locked by Messages.app — log and
            # let the next cycle retry. Cursor stays put.
            _logger.warning(
                "imessage_chat_db_locked",
                extra={"cursor": cursor, "error": str(exc)},
            )
            return cursor

        # ROWID-gap detection: compare cursor to max ROWID in the batch.
        # Gap is measured against max(ROWID) (not row count) so a sparse
        # backlog — e.g. cursor=100, batch=[101, 250, 251] — still trips
        # the WARN at gap=151. Only emit when there are rows; an empty
        # poll cycle is the steady-state idle case and must not log.
        if rows:
            new_max = int(rows[-1]["rowid"])  # query is ORDER BY ROWID ASC
            gap = new_max - cursor
            if gap > ROWID_GAP_WARN_THRESHOLD:
                _logger.warning(
                    "imessage_connector_rowid_gap",
                    extra={
                        "component": "imessage_connector",
                        "reason": "rowid_gap",
                        "last_cursor": cursor,
                        "new_max": new_max,
                        "gap": gap,
                    },
                )

        for row in rows:
            # is_from_me=1: skip. The sink would persist it as outbound,
            # but our outbound path is API-driven, so OS-level outbound
            # copies would double-count.
            if row.get("is_from_me"):
                cursor = int(row["rowid"])  # advance past it (sink not called)
                continue

            channel_id = self._derive_channel_id(row)
            if channel_id is None:
                # Defensive: row had no resolvable handle. Reader's SQL
                # join should prevent this for DM rows, but we don't
                # want a poisoned row to wedge the cursor forever.
                _logger.warning(
                    "imessage_row_skipped_no_channel",
                    extra={"rowid": row.get("rowid")},
                )
                cursor = int(row["rowid"])
                continue

            chat_guid = row.get("chat_guid")
            envelope = await self._build_envelope(row, channel_id)

            # Pre-write channels.metadata_json so the sink's UPSERT (which
            # only refreshes channel_type) preserves the chat_guid we
            # stored. Done outside the sink's transaction so a sink
            # rollback doesn't lose the metadata, but inside an idempotent
            # UPSERT so re-runs of the same row are safe.
            await self._upsert_channel_metadata(
                channel_id=channel_id,
                channel_type=ChannelType.DM,
                chat_guid=chat_guid,
            )

            # Hand off to the sink: persist message + cursor advancement
            # in one transaction. If this raises (duplicate id, FK error,
            # disk full, etc.) the cursor does NOT advance for this row.
            await self._sink.record_inbound(envelope, source=str(row["rowid"]))

            cursor = int(row["rowid"])

        return cursor

    # ------------------------------------------------------------------
    # Inbound helpers
    # ------------------------------------------------------------------

    def _derive_channel_id(self, row: dict[str, Any]) -> str | None:
        """Return the AMG channel_id for an iMessage row.

        Per spec §7.3.1: the iMessage channel_id is the normalized
        E.164 phone or Apple ID email — no ``imessage:`` prefix. The
        reader already normalized ``handle.id``; we use it directly.
        Returns ``None`` when the row has no resolvable sender (rare
        defensive case).
        """
        handle = row.get("sender_handle")
        if not isinstance(handle, str) or not handle:
            return None
        return handle

    async def _build_envelope(
        self,
        row: dict[str, Any],
        channel_id: str,
    ) -> Envelope:
        """Translate a chat.db row dict into a normalized envelope.

        Allowlist resolution happens here (for display_name override)
        AND inside the sink (for the canonical ``allowlist_status``
        snapshot). Both are cheap dict lookups.

        Attachments are re-hosted via the injected
        :class:`AttachmentStore` (when configured). On copy failure the
        envelope ships without that attachment per spec §5.8 edge case.
        """
        sender_id = channel_id  # iMessage: handle == channel == sender id
        entry = self._allowlist.resolve(Source.IMESSAGE, sender_id)
        display_name = (
            entry.display_name
            if entry is not None and entry.display_name
            else sender_id
        )

        sender = Sender(
            id=sender_id,
            display_name=display_name,
            person_id=entry.person_id if entry is not None else None,
        )

        timestamp = row.get("timestamp")
        if not isinstance(timestamp, datetime):
            timestamp = datetime.now(UTC)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        rehosted = await self._rehost_attachments(row.get("attachments") or [])

        text_value = row.get("text") or ""

        raw: dict[str, Any] = {
            "rowid": row.get("rowid"),
            "chat_guid": row.get("chat_guid"),
            "sender_kind": row.get("sender_kind"),
        }
        # Carry the reader's raw_json snapshot when present — it's already
        # JSON-safe (blobs are hex-encoded). Wrapped under a sub-key so
        # ``raw`` stays a flat dict the sink can ``json.dumps``.
        raw_json = row.get("raw_json")
        if isinstance(raw_json, dict):
            raw["chat_db_row"] = raw_json

        return Envelope(
            id=f"msg_{ULID()}",
            source=Source.IMESSAGE,
            channel_id=channel_id,
            channel_type=ChannelType.DM,  # v1: DM only
            sender=sender,
            text=text_value,
            attachments=rehosted,
            reply_to=None,  # iMessage threading is post-v1
            timestamp=timestamp,
            direction=Direction.INBOUND,
            raw=raw,
        )

    async def _rehost_attachments(
        self,
        chat_attachments: Iterable[dict[str, Any]],
    ) -> list[Attachment]:
        """Copy each chat.db attachment into the local store.

        Returns the list of envelope-shaped :class:`Attachment` objects
        for rows that copied successfully. A failed copy logs a WARN
        (inside :meth:`AttachmentStore.rehost`) and is dropped — the
        envelope still ships, sans that attachment, per spec §5.8 edge
        cases.
        """
        if self._attachment_store is None:
            return []

        envelope_attachments: list[Attachment] = []
        for att in chat_attachments:
            source_path = att.get("path")
            if not isinstance(source_path, str) or not source_path:
                continue
            mime = att.get("mime") if isinstance(att.get("mime"), str) else None
            row: AttachmentRow | None = await self._attachment_store.rehost(
                source_path, mime
            )
            if row is None:
                continue
            envelope_attachments.append(
                Attachment(
                    id=row.id,
                    url=self._attachment_store.url_for(row.id),
                    mime=row.mime,
                    size_bytes=row.size_bytes,
                )
            )
        return envelope_attachments

    # ------------------------------------------------------------------
    # connector_state cursor read (startup)
    # ------------------------------------------------------------------

    async def _read_cursor_at_startup(self) -> int | None:
        """Read the persisted cursor for ``source='imessage'``.

        Returns:
            * The stored ROWID (warm restart) — including when the row
              exists but the chat.db has advanced past it (stale-cursor
              case is handled implicitly by the next poll's
              ``WHERE ROWID > cursor`` clause).
            * ``None`` when no row exists for ``source='imessage'``
              (fresh install) or no ``session_factory`` was configured
              (test fakes without a DB). The caller seeds from
              ``MAX(message.ROWID)`` in that case.
            * ``0`` when the stored cursor is malformed (non-integer) —
              treated as a warm restart from the beginning of chat.db
              rather than fresh-install seeding so the operator notices
              the issue (every existing row replays once).

        Raises:
            ``sqlite3.OperationalError`` or ``OSError`` on AMG DB
            connection / IO failures. The caller catches and logs
            ERROR before falling back to fresh-install seeding.
        """
        if self._session_factory is None:
            return None
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT cursor FROM connector_state WHERE source = :source"
                ),
                {"source": Source.IMESSAGE.value},
            )
            row = result.first()
        if row is None:
            return None
        cursor_raw = row[0]
        try:
            return int(cursor_raw)
        except (TypeError, ValueError):
            _logger.warning(
                "imessage_cursor_malformed_at_startup",
                extra={"cursor_raw": cursor_raw},
            )
            return 0

    def _seed_cursor_from_chat_db(self) -> int:
        """Return the current ``MAX(message.ROWID)`` in chat.db, or 0.

        Used as the in-memory starting cursor on fresh install (no
        ``connector_state`` row) so the first poll cycle does not
        replay the entire historical backlog. The seed is never
        written to ``connector_state`` here; the next successful
        :class:`MessageSink` transaction does that atomically with
        the message INSERT.

        Returns 0 when:
        * The reader has no ``path`` attribute (test fakes).
        * Opening / reading chat.db fails (FDA not granted, file
          corrupt, etc.). The poller will then attempt to drain from
          ROWID 0 on its first cycle; this is the conservative
          fallback when we cannot inspect the file.

        Reads are done in a fresh read-only ``sqlite3`` connection so
        the existing :class:`ChatDbReader` connection state is not
        disturbed. The query is a fast index scan on the implicit
        ROWID primary key.
        """
        path: Path | None = getattr(self._reader, "path", None)
        if path is None:
            return 0

        try:
            uri = f"file:{path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            try:
                conn.execute("PRAGMA query_only = ON")
                row = conn.execute(
                    "SELECT COALESCE(MAX(ROWID), 0) FROM message"
                ).fetchone()
            finally:
                conn.close()
        except (sqlite3.OperationalError, OSError) as exc:
            _logger.warning(
                "imessage_seed_cursor_failed_using_zero",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            return 0

        if row is None or row[0] is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # channels.metadata_json upsert (chat.guid pin)
    # ------------------------------------------------------------------

    async def _upsert_channel_metadata(
        self,
        *,
        channel_id: str,
        channel_type: ChannelType,
        chat_guid: str | None,
    ) -> None:
        """UPSERT ``channels`` with metadata_json={"chat_guid": <guid>}.

        Done outside the sink's transaction so the metadata persists even
        if the sink rolls back (the chat.guid is content-addressable per
        channel and won't change). The sink's own UPSERT only refreshes
        ``channel_type`` on conflict, so it preserves the metadata_json
        we wrote here.

        No-op when ``session_factory`` is not configured (test fakes).
        """
        if self._session_factory is None:
            return
        if chat_guid is None:
            metadata_json: str | None = None
        else:
            metadata_json = json.dumps(
                {"chat_guid": chat_guid},
                separators=(",", ":"),
            )

        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO channels
                        (source, channel_id, channel_type, metadata_json)
                    VALUES
                        (:source, :channel_id, :channel_type, :metadata_json)
                    ON CONFLICT(source, channel_id) DO UPDATE SET
                        channel_type = excluded.channel_type,
                        metadata_json = COALESCE(
                            excluded.metadata_json,
                            channels.metadata_json
                        )
                    """
                ),
                {
                    "source": Source.IMESSAGE.value,
                    "channel_id": channel_id,
                    "channel_type": channel_type.value,
                    "metadata_json": metadata_json,
                },
            )

    # ------------------------------------------------------------------
    # Outbound: send
    # ------------------------------------------------------------------

    async def send(
        self,
        channel_id: str,
        text_body: str,
        *,
        reply_to: str | None = None,  # noqa: ARG002 — v1 ignores threading
        attachments: Sequence[Any] | None = None,
    ) -> SendResult:
        """Send an iMessage to a previously-seen channel.

        Resolves the destination ``chat.guid`` from
        ``channels.metadata_json`` (populated by inbound rows). When no
        metadata is found for the channel, falls back to the canonical
        ``iMessage;-;<channel_id>`` shape — that's what AppleScript
        accepts for a DM thread to a known E.164 / email address even
        before any inbound row has populated the metadata.

        ``attachments`` is at most one local file path in v1 (the
        AppleScript template supports a single attachment per send).
        Multiple attachments are sent as the first only — the spec
        treats this as a v1 limitation; the upstream
        ``POST /messages/send`` route validates and rejects multi-attach
        sends before we get here.

        Returns:
            :class:`SendResult` from the underlying AppleScript sender.
            On non-OK we log ERROR; the route translates ``ok=False``
            into ``502 PLATFORM_SEND_FAILED`` per spec §7.4 / §13 R-2.
        """
        chat_guid = await self._resolve_chat_guid(channel_id)
        attachment_path = self._first_attachment_path(attachments)

        result = await self._sender.send(chat_guid, text_body, attachment_path)
        if not result.ok:
            _logger.error(
                "imessage_send_failed",
                extra={
                    "channel_id": channel_id,
                    "chat_guid": chat_guid,
                    "error": result.error,
                },
            )
        return result

    # ------------------------------------------------------------------
    # Outbound helpers
    # ------------------------------------------------------------------

    async def _resolve_chat_guid(self, channel_id: str) -> str:
        """Look up ``chat.guid`` for ``channel_id`` from ``channels.metadata_json``.

        Falls back to ``iMessage;-;<channel_id>`` when no metadata row
        exists or the JSON is malformed. The fallback is the canonical
        shape macOS uses for a DM thread, so AppleScript will route
        correctly even on the first outbound to a never-before-seen
        channel.
        """
        if self._session_factory is None:
            return f"iMessage;-;{channel_id}"

        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT metadata_json FROM channels
                    WHERE source = :source AND channel_id = :channel_id
                    """
                ),
                {
                    "source": Source.IMESSAGE.value,
                    "channel_id": channel_id,
                },
            )
            row = result.first()

        if row is None or row[0] is None:
            return f"iMessage;-;{channel_id}"

        try:
            decoded = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return f"iMessage;-;{channel_id}"

        guid = decoded.get("chat_guid") if isinstance(decoded, dict) else None
        if isinstance(guid, str) and guid:
            return guid
        return f"iMessage;-;{channel_id}"

    def _first_attachment_path(
        self,
        attachments: Sequence[Any] | None,
    ) -> str | None:
        """Return the first attachment's local path, if any.

        Accepts either str paths or objects with a ``path`` attribute /
        key. Returns ``None`` for empty / missing input.
        """
        if not attachments:
            return None
        first = attachments[0]
        if isinstance(first, str):
            return first
        path = getattr(first, "path", None)
        if isinstance(path, str) and path:
            return path
        if isinstance(first, dict):
            value = first.get("path")
            if isinstance(value, str) and value:
                return value
        return None
