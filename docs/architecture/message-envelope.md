# Message Envelope

The **normalized message envelope** is the central contract of AMC. Every message in the system — inbound or outbound, iMessage or Discord — conforms to this one JSON shape.

This is what makes the layering work. A connector's only job is to translate a platform-native payload into (and out of) this envelope. Adding a new platform means writing one connector that emits this shape; the storage layer, the HTTP API, the MCP wrapper, and the agent never change. See the [Architecture overview](index.md) for how the layers fit together and the [Connectors overview](../connectors/index.md) for the translation seam.

!!! info "Where the models live"
    The canonical Pydantic v2 models are defined in `amc/core/envelope.py`. The HTTP API and MCP wrapper serialize `Envelope` instances out; connectors construct them on the way in.

## Annotated example

```jsonc
{
  "id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",       // ^msg_[0-9A-HJKMNP-TV-Z]{26}$
  "source": "imessage",                           // "imessage" | "discord"
  "channel_id": "+15551234567",
  "channel_type": "dm",                           // "dm" | "group"
  "sender": {
    "id": "+15551234567",                         // platform-native sender id
    "display_name": "Alex Allowlisted",           // allowlist override else platform value
    "person_id": "person_alex"                    // string | null; set iff allowlisted
  },
  "text": "Hello!",                               // UTF-8; empty allowed (attachment-only)
  "attachments": [
    {
      "id": "att_01HABCABCDEFGHJKMNPQRSTVWX",     // ^att_[0-9A-HJKMNP-TV-Z]{26}$
      "url": "http://127.0.0.1:8080/attachments/att_01HABC...",
      "mime": "image/png",
      "size_bytes": 12345
    }
  ],
  "reply_to": null,                               // string (msg_<ULID>) | null
  "timestamp": "2026-04-25T15:32:11.123Z",        // RFC 3339 UTC
  "direction": "inbound",                         // "inbound" | "outbound"
  "raw": { "...": "..." }                         // platform-native payload (debug / forward-compat)
}
```

## Field reference

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Adapter-assigned message identifier. ULID with `msg_` prefix; matches `^msg_[0-9A-HJKMNP-TV-Z]{26}$`. |
| `source` | string | Originating platform. One of `imessage`, `discord`. See Enumerations below. |
| `channel_id` | string | Conversation identifier. Non-empty. Discord values are prefixed; iMessage values are not — see channel_id format below. |
| `channel_type` | string | Channel topology. One of `dm`, `group`. See Enumerations below. |
| `sender.id` | string | Platform-native sender id (the same value the platform uses). Non-empty. |
| `sender.display_name` | string | Human-readable name. Never empty — the allowlist override if one exists, otherwise the platform-supplied value. |
| `sender.person_id` | string \| null | Stable cross-platform person reference. Non-null **only** for allowlisted senders; `null` otherwise. |
| `text` | string | UTF-8 message body. May be empty for attachment-only messages. |
| `attachments[]` | array | Zero or more attachments. Each has `id` (`att_<ULID>`), `url`, `mime`, `size_bytes` (see below). |
| `attachments[].id` | string | ULID with `att_` prefix; matches `^att_[0-9A-HJKMNP-TV-Z]{26}$`. |
| `attachments[].url` | string | Adapter-hosted `http(s)` URL — **never** the platform's expiring CDN URL or a local filesystem path. |
| `attachments[].mime` | string | MIME type, e.g. `image/png`. Non-empty. |
| `attachments[].size_bytes` | integer | Size in bytes; `>= 0`. |
| `reply_to` | string \| null | Another `msg_` id within the same channel, or `null`. Threaded replies are a post-v1 feature; populated where the platform makes it available. |
| `timestamp` | string | Platform-claimed message time, RFC 3339 UTC with a literal `Z` suffix — see Timestamp serialization below. |
| `direction` | string | Which way the message moved. One of `inbound`, `outbound`. See Enumerations below. |
| `raw` | object | The JSON-serializable platform-native payload, kept for debugging and forward-compatibility. Not part of the stable contract. |

!!! note "`raw` is intentionally opaque"
    Treat `raw` as a debugging aid, not an API. Its shape is platform-specific and may change as connectors evolve. Everything an agent needs is promoted to a top-level field.

## Enumerations

All four enums are `enum.StrEnum` in `amc/core/envelope.py`, so they serialize as their string value on the wire.

| Enum | Values | Meaning |
| --- | --- | --- |
| `Source` | `imessage`, `discord` | The platform a message originated on. |
| `ChannelType` | `dm`, `group` | Direct message vs. group conversation. v1 iMessage returns only `dm`; Discord returns both. |
| `Direction` | `inbound`, `outbound` | Inbound = received from a sender; outbound = sent by the agent. |
| `AllowlistStatus` | `allowed`, `unknown`, `outbound` | Per-message allowlist snapshot. **Not a wire field** — see below. |

### AllowlistStatus is not on the wire

`AllowlistStatus` is a per-message snapshot persisted alongside the row in storage — it is **not** a field of the JSON envelope. It records the sender's allowlist state at the moment the message was recorded:

- **`allowed`** — the sender was on the allowlist. The message is visible to agents (surfaced in unread).
- **`unknown`** — the sender was not on the allowlist. The message is held in quarantine and is **never** surfaced in unread.
- **`outbound`** — the message is one of the agent's own sends.

Because it is a snapshot taken at record time, later allowlist edits do not retroactively change historical rows. See the [Storage Schema](storage.md) for the column that holds this value.

## Identifiers

Both `id` (message) and each attachment `id` are **ULIDs** rendered in Crockford base32 — a 26-character body whose alphabet excludes the ambiguous letters `I`, `L`, `O`, and `U`. They carry type prefixes so an id is self-describing:

| Kind | Prefix | Regex |
| --- | --- | --- |
| Message | `msg_` | `^msg_[0-9A-HJKMNP-TV-Z]{26}$` |
| Attachment | `att_` | `^att_[0-9A-HJKMNP-TV-Z]{26}$` |

ULIDs are lexicographically sortable by creation time, which keeps message ordering stable in the [Storage Schema](storage.md) and the [REST API](../reference/rest-api.md).

## channel_id format

`channel_id` is the one place where platforms differ at the envelope layer, and the difference is deliberate: the adapter infers `source` from the presence of a `discord:` prefix.

**Discord — always prefixed.** Discord channel ids are prefixed with the channel kind and carry a snowflake:

```text
discord:dm:<snowflake>
discord:channel:<snowflake>
```

**iMessage — never prefixed.** iMessage channel ids carry **no** prefix. They are either an E.164 phone number or an Apple ID email:

```text
+15551234567
alex@example.com
```

!!! tip "Source inference"
    Because only Discord ids carry the `discord:` prefix, the adapter can determine `source` from `channel_id` alone. A bare phone number or email is iMessage; anything beginning with `discord:` is Discord.

For the per-platform details behind these formats, see the [iMessage](../connectors/imessage.md) and [Discord](../connectors/discord.md) connector pages.

## Timestamp serialization

Every adapter-emitted `timestamp` is **RFC 3339 UTC with millisecond precision and a literal `Z` suffix**:

```text
2026-04-25T15:32:11.123Z
```

!!! note "Implementation detail"
    Pydantic v2 does not canonicalize the timezone on validate, so a tz-aware UTC `datetime` would normally serialize with a `+00:00` offset. AMC produces the `Z` form explicitly with a `@field_serializer`:

    ```python
    value.isoformat().replace("+00:00", "Z")
    ```

On **input**, the envelope accepts any RFC 3339 form that `datetime.fromisoformat` can parse; naive datetimes are coerced to UTC rather than rejected. Regardless of the input shape, the adapter always re-emits the canonical `Z`-suffixed form above.

See the [Configuration](../reference/configuration.md) reference for adapter-wide settings and the [REST API](../reference/rest-api.md) for the endpoints that return envelopes.
