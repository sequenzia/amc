"""Normalized message envelope models (spec §7.3.1).

These Pydantic v2 models are the canonical wire shape for every message that
flows through AMC, regardless of platform. Connectors translate platform-native
payloads into an :class:`Envelope` on inbound; the HTTP API and MCP wrapper
serialize :class:`Envelope` instances back out.

Design notes:
- IDs are Crockford-base32 ULIDs (26 chars, alphabet excludes I, L, O, U) with
  type prefixes (`msg_`, `att_`) per spec §7.3.1 / §7.3.3.
- Timestamps are RFC 3339 UTC with `Z` suffix; naive datetimes are coerced to
  UTC rather than rejected (per acceptance criteria).
- :class:`SendMessageRequest` is a separate input model used by
  ``POST /messages/send`` — it permits ``url`` *or* ``path`` per attachment but
  not both.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Source(StrEnum):
    """Originating platform for a message."""

    IMESSAGE = "imessage"
    DISCORD = "discord"


class ChannelType(StrEnum):
    """Channel topology. v1 returns only ``dm`` for iMessage; both for Discord."""

    DM = "dm"
    GROUP = "group"


class Direction(StrEnum):
    """Which way a message moved through AMC."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class AllowlistStatus(StrEnum):
    """Sender allowlist state. Lives on the ``messages`` row, not the envelope."""

    ALLOWED = "allowed"
    UNKNOWN = "unknown"
    OUTBOUND = "outbound"


# ---------------------------------------------------------------------------
# ID validation
# ---------------------------------------------------------------------------

# Crockford base32 alphabet excludes I, L, O, U.
_ULID_BODY = r"[0-9A-HJKMNP-TV-Z]{26}"
MSG_ID_PATTERN = re.compile(rf"^msg_{_ULID_BODY}$")
ATT_ID_PATTERN = re.compile(rf"^att_{_ULID_BODY}$")

MessageId = Annotated[str, StringConstraints(pattern=rf"^msg_{_ULID_BODY}$")]
AttachmentId = Annotated[str, StringConstraints(pattern=rf"^att_{_ULID_BODY}$")]


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _coerce_to_utc(value: Any) -> Any:
    """Pydantic before-validator: coerce naive datetimes to UTC.

    Strings are passed through unchanged so Pydantic's standard ISO 8601 parser
    handles them; we only intervene for ``datetime`` instances that lack a
    timezone.
    """

    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _serialize_timestamp(value: datetime) -> str:
    """Emit RFC 3339 with explicit ``Z`` suffix (never ``+00:00``)."""

    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    # ``isoformat`` on a tz-aware UTC datetime emits ``+00:00``; replace for
    # canonical Z-suffix form per spec §7.3.1 ("2026-04-25T15:32:11Z").
    return value.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Sender(BaseModel):
    """Sender identity carried inside the envelope (spec §7.3.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Platform-native sender id.")
    display_name: str = Field(
        ..., description="Allowlist override else platform value."
    )
    person_id: str | None = Field(
        default=None,
        description="Present iff this sender is allowlisted with a person_id.",
    )


class Attachment(BaseModel):
    """Attachment as it appears on an inbound or outbound envelope.

    URLs are always adapter-hosted on inbound (spec §7.3.1). For the outbound
    *input* shape (where the caller may supply a local file path instead of a
    URL), use :class:`SendAttachmentInput` on :class:`SendMessageRequest`.
    """

    model_config = ConfigDict(extra="forbid")

    id: AttachmentId
    url: str = Field(..., description="Adapter-hosted HTTP(S) URL.")
    mime: str = Field(..., min_length=1)
    size_bytes: int = Field(..., ge=0)

    @field_validator("url")
    @classmethod
    def _validate_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("attachment url must be an http(s) URL")
        return value


class Envelope(BaseModel):
    """Normalized cross-platform message envelope (spec §7.3.1).

    Every inbound and outbound message conforms to this shape. New platforms
    are added by writing one connector that produces an :class:`Envelope`;
    nothing else in the system needs to change.
    """

    model_config = ConfigDict(extra="forbid")

    id: MessageId
    source: Source
    channel_id: str = Field(..., min_length=1)
    channel_type: ChannelType
    sender: Sender
    text: str = Field(..., description="UTF-8; empty allowed for attachment-only.")
    attachments: list[Attachment] = Field(default_factory=list)
    reply_to: MessageId | None = Field(
        default=None,
        description="Another msg_... id within the same channel, if threaded.",
    )
    timestamp: datetime = Field(
        ..., description="Platform-claimed message time (RFC 3339 UTC)."
    )
    direction: Direction
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Platform-native payload for debugging / forward-compat.",
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def _timestamp_to_utc(cls, value: Any) -> Any:
        return _coerce_to_utc(value)

    @field_serializer("timestamp")
    def _serialize_ts(self, value: datetime) -> str:
        return _serialize_timestamp(value)


# ---------------------------------------------------------------------------
# Send-message input model
# ---------------------------------------------------------------------------


class SendAttachmentInput(BaseModel):
    """Outbound attachment supplied by the caller of ``POST /messages/send``.

    Exactly one of ``url`` or ``path`` must be set per attachment.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, description="HTTP(S) URL to fetch.")
    path: str | None = Field(default=None, description="Local filesystem path.")
    mime: str | None = None

    @field_validator("url")
    @classmethod
    def _validate_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("attachment url must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _exactly_one_source(self) -> SendAttachmentInput:
        if (self.url is None) == (self.path is None):
            raise ValueError(
                "attachment must specify exactly one of 'url' or 'path'"
            )
        return self


class SendMessageRequest(BaseModel):
    """Input shape for ``POST /messages/send`` (spec §7.3.1, §5.1).

    The adapter assigns ``id``, ``timestamp``, ``direction='outbound'``, and
    rewrites attachments to the canonical hosted form before persisting.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., min_length=1)
    text: str = Field(..., description="UTF-8; empty allowed for attachment-only.")
    reply_to: MessageId | None = None
    attachments: list[SendAttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_text_or_attachment(self) -> SendMessageRequest:
        if not self.text and not self.attachments:
            raise ValueError("send request must have text or at least one attachment")
        return self


__all__ = [
    "ATT_ID_PATTERN",
    "AllowlistStatus",
    "Attachment",
    "AttachmentId",
    "ChannelType",
    "Direction",
    "Envelope",
    "MSG_ID_PATTERN",
    "MessageId",
    "SendAttachmentInput",
    "SendMessageRequest",
    "Sender",
    "Source",
]
