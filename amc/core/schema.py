"""SQLAlchemy Core schema for the AMC SQLite store (spec §7.3.2 / §7.3.3).

This module defines :data:`metadata` containing all nine tables, indexes, and
``CHECK`` constraints exactly as specified in the AMC spec. It is consumed by:

- The Alembic migration ``001_init`` (calls ``metadata.create_all``).
- The async engine in :mod:`amc.core.db` (used at runtime for queries).
- Tests that introspect the schema via ``sqlite_master``.

Design notes:
- We use SQLAlchemy Core ``Table`` objects (not ORM) because the adapter is a
  small CRUD service with hand-written SQL paths and we want zero ORM magic.
- Every ``CHECK`` constraint and index from §7.3.3 is declared explicitly so a
  future schema audit can grep for them.
- Self-referential and forward-referencing FKs (``messages.reply_to`` and
  ``channels.last_seen_message_id``) are marked ``deferrable=True``,
  ``initially="DEFERRED"`` so circular inserts within a transaction are valid.
  SQLite enforces FKs only when ``PRAGMA foreign_keys = ON`` is set, which
  :mod:`amc.core.db` does at every connection bootstrap.
- The composite FK on ``messages`` to ``(source, channel_id)`` and
  ``(source, sender_id)`` is declared via :class:`ForeignKeyConstraint` because
  per-column ``ForeignKey`` cannot express a composite target.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)

__all__ = [
    "attachments",
    "channels",
    "connector_state",
    "idempotency_keys",
    "identity_links",
    "message_reads",
    "messages",
    "metadata",
    "senders",
    "webhook_deliveries",
]

# Naming convention so Alembic-generated constraint names stay stable across
# environments. Without this, SQLite-named anonymous constraints can drift.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


# ---------------------------------------------------------------------------
# channels  (defined before messages so the composite FK target exists)
# ---------------------------------------------------------------------------

channels = Table(
    "channels",
    metadata,
    Column("source", Text, nullable=False),
    Column("channel_id", Text, nullable=False),
    Column("channel_type", Text, nullable=False),
    Column(
        "last_seen_message_id",
        Text,
        ForeignKey(
            "messages.id",
            name="fk_channels_last_seen_message_id_messages",
            deferrable=True,
            initially="DEFERRED",
            # Mark the channels<->messages cycle so DropAll can sort cleanly.
            # SQLite doesn't ALTER constraints anyway; this hint is for
            # SQLAlchemy's topological sorter.
            use_alter=True,
        ),
        nullable=True,
    ),
    Column("metadata_json", Text, nullable=True),
    PrimaryKeyConstraint("source", "channel_id", name="pk_channels"),
    CheckConstraint(
        "source IN ('imessage','discord')",
        name="ck_channels_source",
    ),
    CheckConstraint(
        "channel_type IN ('dm','group')",
        name="ck_channels_channel_type",
    ),
)


# ---------------------------------------------------------------------------
# senders
# ---------------------------------------------------------------------------

senders = Table(
    "senders",
    metadata,
    Column("source", Text, nullable=False),
    Column("sender_id", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("allowlist_status", Text, nullable=False),
    Column("person_id", Text, nullable=True),
    Column(
        "first_seen",
        Text,
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    ),
    Column(
        "last_seen",
        Text,
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    ),
    PrimaryKeyConstraint("source", "sender_id", name="pk_senders"),
    CheckConstraint(
        "source IN ('imessage','discord')",
        name="ck_senders_source",
    ),
    CheckConstraint(
        "allowlist_status IN ('allowed','unknown')",
        name="ck_senders_allowlist_status",
    ),
    Index("idx_senders_person", "person_id"),
)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

messages = Table(
    "messages",
    metadata,
    Column("id", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("channel_id", Text, nullable=False),
    Column("channel_type", Text, nullable=False),
    Column("sender_id", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column(
        "reply_to",
        Text,
        ForeignKey(
            "messages.id",
            name="fk_messages_reply_to_messages",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    ),
    Column("direction", Text, nullable=False),
    Column("allowlist_status", Text, nullable=False),
    Column("message_ts", Text, nullable=False),
    Column(
        "created_at",
        Text,
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    ),
    Column("attachments_json", Text, nullable=True),
    Column("raw_json", Text, nullable=True),
    # Outbound delivery state (added by migration 002_outbound_status). NULL on
    # inbound rows (the column has no meaning for them); one of
    # ``pending`` / ``sent`` / ``send_failed`` on outbound rows. v1 only writes
    # ``sent`` (outbound rows are created post-send). ``pending`` and
    # ``send_failed`` are reserved for the Phase 2+ async send queue (spec
    # §13 OQ-3) and post-mortem forensics on terminal send failures.
    Column("delivery_status", Text, nullable=True),
    # Composite FKs to (source, channel_id) and (source, sender_id). SQLAlchemy
    # requires a single ForeignKeyConstraint per composite target, and the
    # `source` column is shared between both — that is fine because both FKs
    # constrain the same `source` value to the same row in their respective
    # parent tables.
    ForeignKeyConstraint(
        ["source", "channel_id"],
        ["channels.source", "channels.channel_id"],
        name="fk_messages_source_channel_id_channels",
    ),
    ForeignKeyConstraint(
        ["source", "sender_id"],
        ["senders.source", "senders.sender_id"],
        name="fk_messages_source_sender_id_senders",
    ),
    CheckConstraint(
        "source IN ('imessage','discord')",
        name="ck_messages_source",
    ),
    CheckConstraint(
        "channel_type IN ('dm','group')",
        name="ck_messages_channel_type",
    ),
    CheckConstraint(
        "direction IN ('inbound','outbound')",
        name="ck_messages_direction",
    ),
    # Note: `outbound` is reserved for agent-sent messages where allowlisting
    # does not apply (spec §7.3.3 messages table note).
    CheckConstraint(
        "allowlist_status IN ('allowed','unknown','outbound')",
        name="ck_messages_allowlist_status",
    ),
    # ``delivery_status`` is NULL on inbound rows and one of the three values
    # below on outbound rows (added by migration 002_outbound_status).
    CheckConstraint(
        "delivery_status IS NULL OR "
        "delivery_status IN ('pending','sent','send_failed')",
        name="ck_messages_delivery_status",
    ),
    Index("idx_messages_channel_ts", "channel_id", text("message_ts DESC")),
    Index(
        "idx_messages_allowlist_ts",
        "allowlist_status",
        text("message_ts DESC"),
    ),
    Index("idx_messages_source_created", "source", text("created_at DESC")),
)


# ---------------------------------------------------------------------------
# identity_links
# ---------------------------------------------------------------------------

identity_links = Table(
    "identity_links",
    metadata,
    Column("person_id", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("sender_id", Text, nullable=False),
    PrimaryKeyConstraint("person_id", "source", "sender_id", name="pk_identity_links"),
    ForeignKeyConstraint(
        ["source", "sender_id"],
        ["senders.source", "senders.sender_id"],
        name="fk_identity_links_source_sender_id_senders",
    ),
    CheckConstraint(
        "source IN ('imessage','discord')",
        name="ck_identity_links_source",
    ),
)


# ---------------------------------------------------------------------------
# message_reads
# ---------------------------------------------------------------------------

message_reads = Table(
    "message_reads",
    metadata,
    Column(
        "message_id",
        Text,
        ForeignKey("messages.id", name="fk_message_reads_message_id_messages"),
        nullable=False,
    ),
    Column("agent_id", Text, nullable=False),
    Column("read_at", Text, nullable=False),
    PrimaryKeyConstraint("message_id", "agent_id", name="pk_message_reads"),
    Index("idx_reads_agent", "agent_id", text("read_at DESC")),
)


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------

attachments = Table(
    "attachments",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "message_id",
        Text,
        ForeignKey("messages.id", name="fk_attachments_message_id_messages"),
        nullable=False,
    ),
    Column("mime", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("bytes_path", Text, nullable=True),
    Column("original_url_or_path", Text, nullable=True),
    Column(
        "created_at",
        Text,
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    ),
    Index("idx_attachments_message", "message_id"),
    Index("idx_attachments_created", "created_at"),
)


# ---------------------------------------------------------------------------
# webhook_deliveries
# ---------------------------------------------------------------------------

webhook_deliveries = Table(
    "webhook_deliveries",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "message_id",
        Text,
        ForeignKey("messages.id", name="fk_webhook_deliveries_message_id_messages"),
        nullable=False,
    ),
    Column("attempt", Integer, nullable=False, server_default=text("0")),
    Column("next_retry_at", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("last_response_code", Integer, nullable=True),
    Column("last_error", Text, nullable=True),
    Column(
        "created_at",
        Text,
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    ),
    Column(
        "updated_at",
        Text,
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    ),
    CheckConstraint(
        "status IN ('pending','delivered','dead')",
        name="ck_webhook_deliveries_status",
    ),
    Index("idx_webhook_pending", "status", "next_retry_at"),
)


# ---------------------------------------------------------------------------
# idempotency_keys
# ---------------------------------------------------------------------------

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("key", Text, primary_key=True),
    Column("request_hash", Text, nullable=False),
    Column("response_status", Integer, nullable=False),
    Column("response_body", Text, nullable=False),
    Column("expires_at", Text, nullable=False),
    Index("idx_idem_expires", "expires_at"),
)


# ---------------------------------------------------------------------------
# connector_state
# ---------------------------------------------------------------------------

connector_state = Table(
    "connector_state",
    metadata,
    Column("source", Text, primary_key=True),
    Column("cursor", Text, nullable=False),
    Column(
        "updated_at",
        Text,
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    ),
    CheckConstraint(
        "source IN ('imessage','discord')",
        name="ck_connector_state_source",
    ),
)
