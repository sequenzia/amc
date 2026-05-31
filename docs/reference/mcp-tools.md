# MCP Tools

Cross-reference: spec §7.4.7. The MCP wrapper (`mcp/src/amc_mcp/tools/*.py`)
exposes exactly four tools that map 1:1 to the [REST endpoints](rest-api.md).
Each tool is a self-contained module that registers a `@mcp.tool()` handler with
a Pydantic input schema; none contain platform-specific code (no Discord /
AppleScript / chat.db imports). Input is validated by Pydantic at the framework
boundary; unit tests exercise the same handlers directly with a fake HTTP client.

The wrapper injects the bearer token and `X-Agent-ID` headers from
`AMC_BEARER_TOKEN` / `AMC_AGENT_ID` env vars, and generates a fresh UUIDv4
`Idempotency-Key` for `send_message`. Errors are returned as the spec
§7.4.12 envelope verbatim.

The four tools mirror how a human assistant works: see what's new, look up
context, reply, mark done. New capabilities are meant to be *additive* tools,
not modifications to these four (blueprint §6.1).

---

## `list_unread_messages`

**Maps to**: [`GET /messages/unread`](rest-api.md) (REST §2.1).

**Purpose**: Return allowlisted messages the agent has not yet
acknowledged. Optional filters narrow by source, channel, time, and count.

**Input schema** (Pydantic model → JSON):

```jsonc
{
  "since":       "string?",                        // optional, ISO 8601
  "source":      "\"imessage\" | \"discord\"?",    // optional
  "channel_id":  "string?",                        // optional
  "limit":       "integer? in [1, 100]"            // optional
}
```

**Output** (`content[0].text` is the pretty-printed JSON; `data` carries
the structured form):

```jsonc
{
  "messages":   [ /* Envelope, ... */ ],
  "next_since": "2026-04-25T15:32:11.123Z" // string | null
}
```

**Error envelope** (returned with `isError: true`):

```jsonc
{ "error": { "code": "STABLE_CODE", "message": "...", "status": 401 } }
```

Codes propagate from the adapter: `UNAUTHORIZED`, `AGENT_ID_REQUIRED`,
`VALIDATION_FAILED`.

---

## `send_message`

**Maps to**: [`POST /messages/send`](rest-api.md) (REST §2.5).

**Purpose**: Send a message to a channel. Optional `reply_to` threads under
another message in the same channel. Each attachment must supply exactly
one of `url` (HTTP(S)) or `path` (local file).

**Input schema** (Pydantic model → JSON):

```jsonc
{
  "channel_id": "string",                          // required
  "text":       "string",                          // required (empty allowed)
  "reply_to":   "string?",                         // optional, msg_<ULID>
  "attachments": [                                 // optional array
    {
      "url":  "string?",                           // exactly one of url/path
      "path": "string?"
    }
  ]
}
```

The wrapper attaches `Idempotency-Key: <UUIDv4>` to every call; agents do
not pass keys themselves.

**Output**:

```jsonc
{
  "message_id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",
  "sent_at":    "2026-04-25T15:32:13.123Z"
}
```

**Error envelope** — same shape as `list_unread_messages`. Adapter codes that
surface: `UNAUTHORIZED`, `VALIDATION_FAILED`, `CHANNEL_NOT_FOUND`,
`MESSAGE_NOT_FOUND` (attachment lookup), `ATTACHMENT_TOO_LARGE_FOR_PLATFORM`,
`IDEMPOTENCY_KEY_REUSE`, `RATE_LIMITED` (with `Retry-After` in the upstream
HTTP layer), `PLATFORM_AUTH`, `PLATFORM_SEND_FAILED`.

---

## `mark_read`

**Maps to**: [`POST /messages/mark_read`](rest-api.md) (REST §2.4).

**Purpose**: Acknowledge one or more messages so they no longer appear in
`list_unread_messages`. Naturally idempotent at the storage layer (UPSERT
on `(message_id, agent_id)`); the wrapper does not attach an
`Idempotency-Key`.

**Input schema** (Pydantic model → JSON):

```jsonc
{
  "message_ids": [
    "string matching ^msg_[0-9A-HJKMNP-TV-Z]{26}$"
  ]
}
```

**Output**:

```jsonc
{ "marked_count": 2 }   // count of unique ids submitted (OQ-1)
```

**Error envelope** — same shape as `list_unread_messages`. Adapter codes that
surface: `UNAUTHORIZED`, `AGENT_ID_REQUIRED`, `VALIDATION_FAILED`. Unknown ids do
**not** error — they are silently skipped at the storage layer but still
counted in `marked_count` per OQ-1.

---

## `get_message_context`

**Maps to**: [`GET /messages/context`](rest-api.md) (REST §2.3).

**Purpose**: Fetch N messages before and after a target message in a
channel, in chronological order. Defaults match the spec (`before=5`,
`after=5`); both are capped at 50 in the Pydantic schema so out-of-range inputs
fail fast with a validation error rather than waiting for the adapter to return
422.

**Input schema** (Pydantic model → JSON):

```jsonc
{
  "channel_id":        "string",                                    // required
  "around_message_id": "string matching ^msg_[0-9A-HJKMNP-TV-Z]{26}$", // required
  "before":            "integer in [0, 50] (default 5)",
  "after":             "integer in [0, 50] (default 5)"
}
```

**Output**:

```jsonc
{ "messages": [ /* Envelope, ... */ ] }
```

**Error envelope** — same shape as `list_unread_messages`. Adapter codes that
surface: `UNAUTHORIZED`, `AGENT_ID_REQUIRED`, `VALIDATION_FAILED`,
`MESSAGE_NOT_FOUND` (target unknown / quarantined / channel mismatch).

---

## See also

- [REST API Reference](rest-api.md) — the HTTP endpoints these tools call.
- [Message Envelope](../architecture/message-envelope.md) — the shape every
  `messages[]` entry conforms to.
- The wrapper installation and MCP-host wiring (Claude Desktop, Claude Code,
  Codex CLI) is covered in the [Setup Guide](../getting-started/index.md#9-mcp-wrapper-installation).
