"""Tests for the normalized message envelope (spec §7.3.1).

Imported via the direct module path so we don't depend on `amg.core.__init__`
re-exporting anything (siblings keep that file empty during Wave 3).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from ulid import ULID

from amg.core.envelope import (
    AllowlistStatus,
    Attachment,
    ChannelType,
    Direction,
    Envelope,
    SendAttachmentInput,
    Sender,
    SendMessageRequest,
    Source,
)

# ---------------------------------------------------------------------------
# Helpers / strategies
# ---------------------------------------------------------------------------


def _msg_id() -> str:
    return f"msg_{ULID()}"


def _att_id() -> str:
    return f"att_{ULID()}"


def _spec_envelope_dict() -> dict:
    """The exact envelope from spec §7.3.1, with deterministic IDs."""

    return {
        "id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",
        "source": "imessage",
        "channel_id": "+15551234567",
        "channel_type": "dm",
        "sender": {
            "id": "+15551234567",
            "display_name": "Alice",
            "person_id": "alice",
        },
        "text": "hey, can you check the build?",
        "attachments": [
            {
                "id": "att_01HABCDEFGHJKMNPQRSTVWXYZ0",
                "url": "http://127.0.0.1:8080/attachments/att_01HABCDEFGHJKMNPQRSTVWXYZ0",
                "mime": "image/png",
                "size_bytes": 124356,
            }
        ],
        "reply_to": None,
        "timestamp": "2026-04-25T15:32:11Z",
        "direction": "inbound",
        "raw": {"...": "platform-native object for debugging"},
    }


# Hypothesis strategies: build an envelope from random valid pieces.
_text_strategy = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),  # no surrogates
    max_size=200,
)
_channel_id_strategy = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=64,
)
_display_name_strategy = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=64,
)
_timestamp_strategy = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 1, 1),
    timezones=st.just(UTC),
)


@st.composite
def envelope_strategy(draw) -> Envelope:
    has_attachment = draw(st.booleans())
    attachments: list[Attachment] = []
    if has_attachment:
        attachments.append(
            Attachment(
                id=_att_id(),
                url=draw(
                    st.sampled_from(
                        [
                            "http://127.0.0.1:8080/attachments/x",
                            "https://example.com/file.png",
                            "https://cdn.test/image.jpg",
                        ]
                    )
                ),
                mime=draw(st.sampled_from(["image/png", "image/jpeg", "text/plain"])),
                size_bytes=draw(st.integers(min_value=0, max_value=10_000_000)),
            )
        )

    has_reply = draw(st.booleans())

    return Envelope(
        id=_msg_id(),
        source=draw(st.sampled_from(list(Source))),
        channel_id=draw(_channel_id_strategy),
        channel_type=draw(st.sampled_from(list(ChannelType))),
        sender=Sender(
            id=draw(_channel_id_strategy),
            display_name=draw(_display_name_strategy),
            person_id=draw(st.one_of(st.none(), st.text(min_size=1, max_size=32))),
        ),
        text=draw(_text_strategy),
        attachments=attachments,
        reply_to=_msg_id() if has_reply else None,
        timestamp=draw(_timestamp_strategy),
        direction=draw(st.sampled_from(list(Direction))),
        raw=draw(
            st.dictionaries(
                keys=st.text(min_size=1, max_size=8),
                values=st.one_of(st.integers(), st.text(max_size=16), st.booleans()),
                max_size=4,
            )
        ),
    )


# ---------------------------------------------------------------------------
# Functional: round-trip the spec example
# ---------------------------------------------------------------------------


def test_spec_example_envelope_round_trips() -> None:
    raw = _spec_envelope_dict()

    parsed = Envelope.model_validate(raw)
    dumped = parsed.model_dump(mode="json")
    reparsed = Envelope.model_validate(dumped)

    assert reparsed == parsed
    # Timestamp preserved as Z-suffixed RFC 3339.
    assert dumped["timestamp"] == "2026-04-25T15:32:11Z"
    # Sender / attachment fidelity preserved.
    assert dumped["sender"]["person_id"] == "alice"
    assert dumped["attachments"][0]["size_bytes"] == 124356


def test_spec_example_envelope_json_round_trips() -> None:
    raw = _spec_envelope_dict()
    parsed = Envelope.model_validate(raw)
    serialized = parsed.model_dump_json()
    reparsed = Envelope.model_validate_json(serialized)
    assert reparsed == parsed


# ---------------------------------------------------------------------------
# Functional: hypothesis property test
# ---------------------------------------------------------------------------


@settings(max_examples=75, deadline=None)
@given(env=envelope_strategy())
def test_random_envelope_round_trips_through_json(env: Envelope) -> None:
    serialized = env.model_dump_json()
    # Must be valid JSON.
    json.loads(serialized)
    reparsed = Envelope.model_validate_json(serialized)
    assert reparsed == env


# ---------------------------------------------------------------------------
# Functional: validation of enums and ID prefixes
# ---------------------------------------------------------------------------


def _valid_payload() -> dict:
    return _spec_envelope_dict()


def test_invalid_direction_rejected() -> None:
    payload = _valid_payload()
    payload["direction"] = "sideways"
    with pytest.raises(ValidationError) as excinfo:
        Envelope.model_validate(payload)
    assert "direction" in str(excinfo.value)


def test_invalid_source_rejected() -> None:
    payload = _valid_payload()
    payload["source"] = "telegram"
    with pytest.raises(ValidationError) as excinfo:
        Envelope.model_validate(payload)
    assert "source" in str(excinfo.value)


def test_invalid_channel_type_rejected() -> None:
    payload = _valid_payload()
    payload["channel_type"] = "broadcast"
    with pytest.raises(ValidationError) as excinfo:
        Envelope.model_validate(payload)
    assert "channel_type" in str(excinfo.value)


def test_invalid_id_prefix_rejected() -> None:
    payload = _valid_payload()
    payload["id"] = "xxx_01HXYZABCDEFGHJKMNPQRSTVWX"
    with pytest.raises(ValidationError) as excinfo:
        Envelope.model_validate(payload)
    assert "id" in str(excinfo.value)


def test_id_with_disallowed_crockford_char_rejected() -> None:
    # 'I' is excluded from the Crockford ULID alphabet.
    payload = _valid_payload()
    payload["id"] = "msg_0IHXYZABCDEFGHJKMNPQRSTVWX"
    with pytest.raises(ValidationError):
        Envelope.model_validate(payload)


def test_invalid_attachment_id_prefix_rejected() -> None:
    payload = _valid_payload()
    payload["attachments"][0]["id"] = "msg_01HABCDEFGHJKMNPQRSTVWXYZ0"
    with pytest.raises(ValidationError) as excinfo:
        Envelope.model_validate(payload)
    assert "id" in str(excinfo.value)


def test_attachment_url_must_be_http() -> None:
    payload = _valid_payload()
    payload["attachments"][0]["url"] = "file:///etc/passwd"
    with pytest.raises(ValidationError) as excinfo:
        Envelope.model_validate(payload)
    assert "url" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_text_allowed_with_attachment() -> None:
    payload = _valid_payload()
    payload["text"] = ""
    env = Envelope.model_validate(payload)
    assert env.text == ""
    assert len(env.attachments) == 1


def test_empty_text_allowed_without_attachment() -> None:
    payload = _valid_payload()
    payload["text"] = ""
    payload["attachments"] = []
    env = Envelope.model_validate(payload)
    assert env.text == ""
    assert env.attachments == []


def test_reply_to_may_be_null() -> None:
    payload = _valid_payload()
    payload["reply_to"] = None
    env = Envelope.model_validate(payload)
    assert env.reply_to is None


def test_reply_to_accepts_valid_msg_id() -> None:
    payload = _valid_payload()
    parent = _msg_id()
    payload["reply_to"] = parent
    env = Envelope.model_validate(payload)
    assert env.reply_to == parent


def test_reply_to_rejects_invalid_id() -> None:
    payload = _valid_payload()
    payload["reply_to"] = "not-a-ulid"
    with pytest.raises(ValidationError):
        Envelope.model_validate(payload)


def test_send_request_attachment_url_and_path_mutually_exclusive() -> None:
    with pytest.raises(ValidationError) as excinfo:
        SendAttachmentInput(url="https://example.com/x.png", path="/tmp/x.png")
    assert "exactly one" in str(excinfo.value)


def test_send_request_attachment_requires_url_or_path() -> None:
    with pytest.raises(ValidationError) as excinfo:
        SendAttachmentInput()
    assert "exactly one" in str(excinfo.value)


def test_send_request_attachment_url_only_ok() -> None:
    a = SendAttachmentInput(url="https://example.com/x.png")
    assert a.url == "https://example.com/x.png"
    assert a.path is None


def test_send_request_attachment_path_only_ok() -> None:
    a = SendAttachmentInput(path="/tmp/x.png")
    assert a.path == "/tmp/x.png"
    assert a.url is None


def test_send_request_text_only_ok() -> None:
    req = SendMessageRequest(channel_id="+15551234567", text="hello")
    assert req.attachments == []


def test_send_request_attachment_only_ok() -> None:
    req = SendMessageRequest(
        channel_id="+15551234567",
        text="",
        attachments=[SendAttachmentInput(path="/tmp/x.png")],
    )
    assert req.text == ""
    assert len(req.attachments) == 1


def test_send_request_empty_text_no_attachments_rejected() -> None:
    with pytest.raises(ValidationError):
        SendMessageRequest(channel_id="+15551234567", text="")


def test_send_request_reply_to_validates_prefix() -> None:
    with pytest.raises(ValidationError):
        SendMessageRequest(
            channel_id="c", text="x", reply_to="bad_01HXYZABCDEFGHJKMNPQRSTVWX"
        )


# ---------------------------------------------------------------------------
# Error handling: timestamp coercion
# ---------------------------------------------------------------------------


def test_naive_datetime_coerced_to_utc() -> None:
    payload = _valid_payload()
    payload["timestamp"] = datetime(2026, 4, 25, 15, 32, 11)  # naive
    env = Envelope.model_validate(payload)
    assert env.timestamp.tzinfo is not None
    assert env.timestamp.utcoffset() == timedelta(0)
    dumped = env.model_dump(mode="json")
    assert dumped["timestamp"] == "2026-04-25T15:32:11Z"


def test_aware_non_utc_datetime_serialized_as_z() -> None:
    payload = _valid_payload()
    # 15:32:11 in +05:00 is 10:32:11 UTC.
    payload["timestamp"] = datetime(2026, 4, 25, 15, 32, 11, tzinfo=timezone(timedelta(hours=5)))
    env = Envelope.model_validate(payload)
    dumped = env.model_dump(mode="json")
    assert dumped["timestamp"] == "2026-04-25T10:32:11Z"


def test_iso_string_with_z_parsed() -> None:
    payload = _valid_payload()
    payload["timestamp"] = "2026-04-25T15:32:11Z"
    env = Envelope.model_validate(payload)
    assert env.timestamp == datetime(2026, 4, 25, 15, 32, 11, tzinfo=UTC)


def test_iso_string_with_offset_serializes_as_utc_z() -> None:
    payload = _valid_payload()
    payload["timestamp"] = "2026-04-25T15:32:11+05:00"
    env = Envelope.model_validate(payload)
    # Pydantic preserves the parsed offset on the in-memory datetime; the
    # serializer is what canonicalizes to UTC + Z suffix.
    dumped = env.model_dump(mode="json")
    assert dumped["timestamp"] == "2026-04-25T10:32:11Z"


# ---------------------------------------------------------------------------
# Enum smoke
# ---------------------------------------------------------------------------


def test_enum_values_match_spec() -> None:
    assert {s.value for s in Source} == {"imessage", "discord"}
    assert {c.value for c in ChannelType} == {"dm", "group"}
    assert {d.value for d in Direction} == {"inbound", "outbound"}
    assert {a.value for a in AllowlistStatus} == {"allowed", "unknown", "outbound"}
