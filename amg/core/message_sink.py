"""Single-transaction sink for normalized messages (spec §5.1, §5.5, §5.7).

The :class:`MessageSink` is the sole chokepoint where connectors hand off a
:class:`amg.core.envelope.Envelope` for persistence. Centralizing the write
path here means:

* Allowlist resolution + ``allowlist_status`` snapshotting happens in one
  place — connectors don't have to re-implement it (spec §5.7).
* Webhook fan-out (``webhook_deliveries`` row) is enqueued atomically with
  the ``messages`` row (spec §5.5 acceptance criteria).
* Connector cursor advancement (``connector_state.cursor``) is part of the
  same transaction as the message INSERT (spec §5.1 technical note: "Each
  connector's last-seen cursor lives in a row of the adapter's
  ``connector_state`` table, updated transactionally with the message
  INSERT").

Webhook gating (spec §5.5 acceptance + OQ-2)
--------------------------------------------
A ``webhook_deliveries`` row is enqueued only when **all three** are true:

1. ``envelope.direction == 'inbound'`` (OQ-2: outbound never fires v1).
2. ``allowlist_status == 'allowed'`` (quarantined ``unknown`` never fires).
3. ``AMG_WEBHOOK_URL`` is set, surfaced as a non-``None``
   :class:`amg.core.webhook.WebhookConfig` on the sink.

If the sink is constructed without a ``webhook_config``, no webhook rows are
created regardless of direction or allowlist status — matching the spec
"if unset, no ``webhook_deliveries`` rows are created" criterion.

Concurrency
-----------
Each ``record_inbound`` / ``record_outbound`` call opens a fresh
:class:`AsyncSession`, runs every UPSERT/INSERT/UPDATE inside one
``BEGIN IMMEDIATE``-style transaction (SQLAlchemy's ``session.begin()`` on
SQLite + WAL), and commits at the end. Concurrent connectors (Discord +
iMessage running in parallel) serialize on SQLite's writer lock for the
duration of one short transaction; WAL mode (set by
:func:`amg.core.db.register_sqlite_pragmas`) keeps readers unblocked.

Public surface
--------------
* :class:`MessageSink` — instantiate once at adapter startup with the
  session factory, allowlist loader, and (optional) webhook config; pass
  to every connector.
* :meth:`MessageSink.record_inbound` — connector inbound path.
* :meth:`MessageSink.record_outbound` — adapter outbound path
  (``POST /messages/send``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from amg.core.allowlist import AllowlistLoader
from amg.core.envelope import AllowlistStatus, Direction, Envelope
from amg.core.webhook import WebhookConfig, enqueue_webhook

__all__ = ["MessageSink"]

_log = structlog.get_logger("amg.message_sink")


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso_z(dt: datetime) -> str:
    """Microsecond-width ISO 8601 UTC with ``Z`` suffix (lex-sortable)."""
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _serialize_envelope_ts(envelope: Envelope) -> str:
    """Render the envelope's platform timestamp the same way the wire model does."""
    ts = envelope.timestamp
    aware = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _attachments_json(envelope: Envelope) -> str | None:
    """Denormalized attachment JSON for ``messages.attachments_json``.

    Returns ``None`` when there are no attachments so the column stays NULL
    (matches the schema's ``nullable=True`` default and the test fixtures in
    ``tests/core/test_webhook.py``).
    """
    if not envelope.attachments:
        return None
    payload = [
        {
            "id": att.id,
            "url": att.url,
            "mime": att.mime,
            "size_bytes": att.size_bytes,
        }
        for att in envelope.attachments
    ]
    return json.dumps(payload, separators=(",", ":"))


def _raw_json(envelope: Envelope) -> str | None:
    """Serialize ``envelope.raw`` for ``messages.raw_json``; ``None`` if empty."""
    if not envelope.raw:
        return None
    return json.dumps(envelope.raw, separators=(",", ":"), default=str)


class MessageSink:
    """Single-transaction persistence chokepoint for inbound/outbound messages.

    Construct once at adapter startup and share across connectors. Every
    method opens its own short-lived :class:`AsyncSession`, runs the entire
    write path inside one transaction, and commits before returning.
    """

    __slots__ = (
        "_session_factory",
        "_allowlist",
        "_webhook_config",
        "_url_snapshot",
        "_time_provider",
    )

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        allowlist: AllowlistLoader,
        webhook_config: WebhookConfig | None = None,
        url_snapshot: dict[str, str] | None = None,
        time_provider: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Wire up the sink.

        Args:
            session_factory: From :func:`amg.core.db.create_session_factory`.
            allowlist: Loaded :class:`AllowlistLoader`. Used to resolve the
                ``allowlist_status`` snapshot at INSERT time (spec §5.7).
            webhook_config: ``None`` disables webhook fan-out entirely
                (matches ``AMG_WEBHOOK_URL`` unset). When set, inbound
                allowlisted messages enqueue a delivery row.
            url_snapshot: Pass the running :class:`amg.core.webhook.WebhookWorker`'s
                ``url_snapshot`` map so per-delivery URLs are pinned for the
                worker's lifetime. Tests can supply a fresh dict.
            time_provider: Injectable wall clock for deterministic tests.
        """
        self._session_factory = session_factory
        self._allowlist = allowlist
        self._webhook_config = webhook_config
        self._url_snapshot = url_snapshot if url_snapshot is not None else {}
        self._time_provider = time_provider

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def record_inbound(self, envelope: Envelope, source: str) -> str:
        """Persist an inbound envelope and (conditionally) enqueue a webhook.

        ``source`` is the connector's current cursor value (last-seen
        ``ROWID`` for iMessage, last sequence number for Discord). It is
        written to ``connector_state.cursor`` for ``connector_state.source =
        envelope.source`` inside the same transaction as the message INSERT,
        per spec §5.1 technical note.

        Returns:
            The persisted ``messages.id`` (== ``envelope.id``).

        Raises:
            ValueError: If ``envelope.direction != 'inbound'``.
        """
        if envelope.direction != Direction.INBOUND:
            raise ValueError(
                "record_inbound called with envelope.direction="
                f"{envelope.direction!r}; use record_outbound for outbound."
            )

        allowlist_status = self._resolve_inbound_allowlist_status(envelope)
        return await self._record(
            envelope,
            cursor=source,
            allowlist_status=allowlist_status,
            enqueue_webhook_row=(
                allowlist_status is AllowlistStatus.ALLOWED
                and self._webhook_config is not None
            ),
        )

    async def record_outbound(self, envelope: Envelope, source: str) -> str:
        """Persist an outbound envelope. Never enqueues a webhook (OQ-2).

        ``source`` semantics match :meth:`record_inbound`: the connector
        cursor value to write into ``connector_state.cursor``. For the
        outbound path the cursor is normally the just-sent message id (or
        platform-acknowledged id), but the sink is opaque to its meaning.

        Returns:
            The persisted ``messages.id`` (== ``envelope.id``).

        Raises:
            ValueError: If ``envelope.direction != 'outbound'``.
        """
        if envelope.direction != Direction.OUTBOUND:
            raise ValueError(
                "record_outbound called with envelope.direction="
                f"{envelope.direction!r}; use record_inbound for inbound."
            )

        # Outbound messages always carry the reserved 'outbound' status on
        # the messages row (spec §7.3.3 messages.allowlist_status note).
        return await self._record(
            envelope,
            cursor=source,
            allowlist_status=AllowlistStatus.OUTBOUND,
            enqueue_webhook_row=False,
        )

    # ------------------------------------------------------------------
    # Allowlist resolution
    # ------------------------------------------------------------------

    def _resolve_inbound_allowlist_status(self, envelope: Envelope) -> AllowlistStatus:
        """Look up the sender in the loader; ``allowed`` iff present.

        Per spec §5.7: "Allowlist resolution happens once per message at
        INSERT time". This is that resolution. The loader's in-memory map
        is the authority; the ``senders.allowlist_status`` row (which we
        upsert in the same transaction) is a denormalized cache.
        """
        entry = self._allowlist.resolve(envelope.source, envelope.sender.id)
        return AllowlistStatus.ALLOWED if entry is not None else AllowlistStatus.UNKNOWN

    # ------------------------------------------------------------------
    # The single transaction
    # ------------------------------------------------------------------

    async def _record(
        self,
        envelope: Envelope,
        *,
        cursor: str,
        allowlist_status: AllowlistStatus,
        enqueue_webhook_row: bool,
    ) -> str:
        """Run the full upsert/insert/cursor/webhook sequence in one tx."""
        message_ts = _serialize_envelope_ts(envelope)
        attachments_json = _attachments_json(envelope)
        raw_json = _raw_json(envelope)
        now_iso = _to_iso_z(self._time_provider())

        async with self._session_factory() as session, session.begin():
            # 1. UPSERT senders. For inbound, snapshot the resolved
            #    allowlist_status so the senders row matches the
            #    messages row. For outbound the sender is the agent /
            #    "us"; we still upsert with a sensible status
            #    (treat as 'allowed' if the loader knows the id, else
            #    'unknown' — outbound is never returned to consumers
            #    so the status is informational here).
            sender_status = self._sender_status_for_upsert(envelope, allowlist_status)
            await _upsert_sender(
                session,
                source=envelope.source.value,
                sender_id=envelope.sender.id,
                display_name=envelope.sender.display_name,
                person_id=envelope.sender.person_id,
                allowlist_status=sender_status.value,
                now_iso=now_iso,
            )

            # 2. UPSERT channels.
            await _upsert_channel(
                session,
                source=envelope.source.value,
                channel_id=envelope.channel_id,
                channel_type=envelope.channel_type.value,
            )

            # 3. INSERT messages row first so the FK target exists for
            #    attachments. (Schema declares attachments.message_id
            #    NOT NULL FK to messages.id.)
            await _insert_message(
                session,
                envelope=envelope,
                allowlist_status=allowlist_status.value,
                message_ts=message_ts,
                attachments_json=attachments_json,
                raw_json=raw_json,
            )

            # 4. UPSERT attachments rows. Done inside the same tx so a
            #    failure in any attachment INSERT rolls back the
            #    message + sender + channel writes too.
            for att in envelope.attachments:
                await _upsert_attachment(
                    session,
                    attachment_id=att.id,
                    message_id=envelope.id,
                    mime=att.mime,
                    size_bytes=att.size_bytes,
                    original_url=att.url,
                )

            # 5. UPDATE connector_state.cursor for this source.
            await _upsert_connector_cursor(
                session,
                source=envelope.source.value,
                cursor=cursor,
                now_iso=now_iso,
            )

            # 6. Conditionally enqueue webhook delivery row. Gating is
            #    decided by the caller (record_inbound only enables it
            #    for allowed-inbound + configured URL).
            if enqueue_webhook_row:
                await enqueue_webhook(
                    session,
                    envelope.id,
                    config=self._webhook_config,
                    url_snapshot=self._url_snapshot,
                    time_provider=self._time_provider,
                )

        _log.info(
            "message_persisted",
            message_id=envelope.id,
            source=envelope.source.value,
            direction=envelope.direction.value,
            allowlist_status=allowlist_status.value,
            attachments=len(envelope.attachments),
            webhook_enqueued=enqueue_webhook_row,
        )
        return envelope.id

    def _sender_status_for_upsert(
        self,
        envelope: Envelope,
        message_allowlist_status: AllowlistStatus,
    ) -> AllowlistStatus:
        """Derive the value to write into ``senders.allowlist_status``.

        The senders table CHECK constraint accepts only ``'allowed'`` or
        ``'unknown'`` (no ``'outbound'``). For inbound the status mirrors
        the message-level snapshot. For outbound we look up the sender;
        unknown outbound senders default to ``'unknown'`` so the row is
        still creatable.
        """
        if envelope.direction is Direction.INBOUND:
            return message_allowlist_status
        # Outbound: defer to the loader if it knows about this sender id;
        # otherwise mark unknown (the row is needed for the FK from the
        # messages row regardless).
        entry = self._allowlist.resolve(envelope.source, envelope.sender.id)
        return AllowlistStatus.ALLOWED if entry is not None else AllowlistStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Per-table UPSERT/INSERT helpers
# ---------------------------------------------------------------------------


async def _upsert_sender(
    session: AsyncSession,
    *,
    source: str,
    sender_id: str,
    display_name: str,
    person_id: str | None,
    allowlist_status: str,
    now_iso: str,
) -> None:
    """SQLite UPSERT into ``senders`` — refresh ``last_seen`` on conflict.

    First-seen and last-seen behavior:

    * On INSERT (new sender), both ``first_seen`` and ``last_seen`` are set
      to ``now_iso`` so the rows are deterministic in tests.
    * On CONFLICT (existing sender), ``last_seen`` advances and the
      identity fields (``display_name``, ``person_id``,
      ``allowlist_status``) are refreshed from the latest envelope —
      consistent with allowlist edits between adapter restarts.
    """
    await session.execute(
        text(
            """
            INSERT INTO senders
                (source, sender_id, display_name, allowlist_status,
                 person_id, first_seen, last_seen)
            VALUES
                (:source, :sender_id, :display_name, :allowlist_status,
                 :person_id, :now, :now)
            ON CONFLICT(source, sender_id) DO UPDATE SET
                display_name = excluded.display_name,
                allowlist_status = excluded.allowlist_status,
                person_id = excluded.person_id,
                last_seen = excluded.last_seen
            """
        ),
        {
            "source": source,
            "sender_id": sender_id,
            "display_name": display_name,
            "allowlist_status": allowlist_status,
            "person_id": person_id,
            "now": now_iso,
        },
    )


async def _upsert_channel(
    session: AsyncSession,
    *,
    source: str,
    channel_id: str,
    channel_type: str,
) -> None:
    """SQLite UPSERT into ``channels``; ``last_seen_message_id`` left to caller.

    The composite PK is ``(source, channel_id)``. ``channel_type`` is
    refreshed on conflict to handle a (rare) DM ↔ group classification
    correction.
    """
    await session.execute(
        text(
            """
            INSERT INTO channels
                (source, channel_id, channel_type)
            VALUES
                (:source, :channel_id, :channel_type)
            ON CONFLICT(source, channel_id) DO UPDATE SET
                channel_type = excluded.channel_type
            """
        ),
        {
            "source": source,
            "channel_id": channel_id,
            "channel_type": channel_type,
        },
    )


async def _insert_message(
    session: AsyncSession,
    *,
    envelope: Envelope,
    allowlist_status: str,
    message_ts: str,
    attachments_json: str | None,
    raw_json: str | None,
) -> None:
    """INSERT a row into ``messages``; ``created_at`` defaults to ``now``.

    ``id`` is the envelope id; the caller is responsible for ensuring it
    is unique (the connector mints a fresh ULID). A duplicate id raises
    ``IntegrityError`` and rolls back the surrounding transaction — the
    connector treats this as a "message already ingested" no-op.
    """
    await session.execute(
        text(
            """
            INSERT INTO messages
                (id, source, channel_id, channel_type, sender_id,
                 text, reply_to, direction, allowlist_status,
                 message_ts, attachments_json, raw_json)
            VALUES
                (:id, :source, :channel_id, :channel_type, :sender_id,
                 :text, :reply_to, :direction, :allowlist_status,
                 :message_ts, :attachments_json, :raw_json)
            """
        ),
        {
            "id": envelope.id,
            "source": envelope.source.value,
            "channel_id": envelope.channel_id,
            "channel_type": envelope.channel_type.value,
            "sender_id": envelope.sender.id,
            "text": envelope.text,
            "reply_to": envelope.reply_to,
            "direction": envelope.direction.value,
            "allowlist_status": allowlist_status,
            "message_ts": message_ts,
            "attachments_json": attachments_json,
            "raw_json": raw_json,
        },
    )


async def _upsert_attachment(
    session: AsyncSession,
    *,
    attachment_id: str,
    message_id: str,
    mime: str,
    size_bytes: int,
    original_url: str,
) -> None:
    """SQLite UPSERT into ``attachments``.

    ``original_url_or_path`` stores the inbound URL as supplied by the
    connector (Discord CDN URL, iMessage local path, etc.). The
    adapter-hosted ``bytes_path`` is populated by the attachment copier
    (task #49); this sink leaves it NULL.
    """
    await session.execute(
        text(
            """
            INSERT INTO attachments
                (id, message_id, mime, size_bytes, original_url_or_path)
            VALUES
                (:id, :message_id, :mime, :size_bytes, :original)
            ON CONFLICT(id) DO UPDATE SET
                mime = excluded.mime,
                size_bytes = excluded.size_bytes,
                original_url_or_path = excluded.original_url_or_path
            """
        ),
        {
            "id": attachment_id,
            "message_id": message_id,
            "mime": mime,
            "size_bytes": size_bytes,
            "original": original_url,
        },
    )


async def _upsert_connector_cursor(
    session: AsyncSession,
    *,
    source: str,
    cursor: str,
    now_iso: str,
) -> None:
    """SQLite UPSERT into ``connector_state``; ``cursor`` is opaque text.

    Spec §5.1: the cursor advance is part of the same transaction as the
    message INSERT so a crash mid-transaction can never leave the cursor
    ahead of the persisted messages.
    """
    await session.execute(
        text(
            """
            INSERT INTO connector_state
                (source, cursor, updated_at)
            VALUES
                (:source, :cursor, :now)
            ON CONFLICT(source) DO UPDATE SET
                cursor = excluded.cursor,
                updated_at = excluded.updated_at
            """
        ),
        {
            "source": source,
            "cursor": cursor,
            "now": now_iso,
        },
    )


