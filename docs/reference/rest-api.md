# REST API Reference

Cross-reference: this document expands the contract defined in
[`specs/agent-messaging-gateway-SPEC.md` §7.4](https://github.com/sequenzia/amg/blob/main/specs/agent-messaging-gateway-SPEC.md#74-api-specifications)
("API Specifications"). It is regenerated from the FastAPI app's OpenAPI schema
(`amg.app.app.openapi()`). The parallel MCP surface is documented separately in
[MCP Tools](mcp-tools.md).

The adapter exposes two parallel surfaces:

1. **REST** — `amg.app:app`, an HTTP API bound to `127.0.0.1:8080` by default
   (configurable via `AMG_BIND_HOST` / `AMG_BIND_PORT`). Every route is gated
   by an `Authorization: Bearer <token>` header; per-agent read endpoints
   additionally require an `X-Agent-ID: <name>` header. Documented on this page.
2. **MCP** — a thin Python wrapper (`mcp/`, FastMCP) that exposes four
   tools to MCP-speaking agents and translates each call into one HTTP
   request against the REST surface. The wrapper injects bearer / agent-id
   headers and an `Idempotency-Key` for `send_message`. See [MCP Tools](mcp-tools.md).

---

## 1. Conventions

### 1.1 Authentication

All endpoints require:

```
Authorization: Bearer ${AMG_BEARER_TOKEN}
```

Read endpoints that filter by allowlist visibility additionally require:

```
X-Agent-ID: ${AMG_AGENT_ID}
```

The header requirements per endpoint follow spec §7.4 (header table). Missing
or invalid bearer returns `401 UNAUTHORIZED`; missing `X-Agent-ID` on a
per-agent endpoint returns `400 AGENT_ID_REQUIRED`.

### 1.2 Standard error envelope (spec §7.4.12)

Every non-2xx response (except `204` and `429`) returns the same envelope:

```json
{
  "error": {
    "code": "STABLE_CODE",
    "message": "Human-readable explanation",
    "details": { "field": "value" }
  }
}
```

Stable codes used across the surface:

| Code | Status | Meaning |
|------|--------|---------|
| `UNAUTHORIZED` | 401 | Bearer header missing or token mismatch |
| `AGENT_ID_REQUIRED` | 400 | `X-Agent-ID` header missing on a per-agent endpoint |
| `VALIDATION_FAILED` | 400 / 422 | Body / query / path validation failed (spec uses `VALIDATION_ERROR`; the implementation emits `VALIDATION_FAILED` for the AMG-flavored cases) |
| `IDEMPOTENCY_KEY_REUSE` | 422 | Same `Idempotency-Key` reused with a different body hash |
| `RATE_LIMITED` | 429 | Per-channel token bucket exhausted (`Retry-After` header set) |
| `MESSAGE_NOT_FOUND` | 404 | Message id unknown OR not visible (e.g. quarantined) |
| `CHANNEL_NOT_FOUND` | 404 | `channel_id` not present in the `channels` table |
| `ATTACHMENT_TOO_LARGE_FOR_PLATFORM` | 413 | Attachment exceeds platform limit (Phase 1: any Discord attachment) |
| `PLATFORM_AUTH` | 502 | Platform rejected the bot/sender credentials |
| `PLATFORM_SEND_FAILED` | 502 | Platform send failed after the connector exhausted its retries |
| `INTERNAL_ERROR` | 500 | Unhandled exception — see structured logs |

### 1.3 Identifier formats

- **Message id** — `msg_<26 Crockford-base32 chars>` (ULID body; alphabet
  excludes I, L, O, U). Regex: `^msg_[0-9A-HJKMNP-TV-Z]{26}$`.
- **Attachment id** — `att_<26 Crockford-base32 chars>`. Regex:
  `^att_[0-9A-HJKMNP-TV-Z]{26}$`.
- **Channel id** — platform-specific. Discord channels are prefixed
  (`discord:dm:<id>` / `discord:channel:<id>`); iMessage channels are an E.164
  phone number (`+15551234567`) or a chat GUID. The adapter infers the
  source from the `discord:` prefix.

### 1.4 Timestamp format

All adapter-emitted timestamps are RFC 3339 UTC with millisecond precision
and a literal `Z` suffix, e.g. `2026-04-25T15:32:11.123Z`. Inputs accept any
RFC 3339 form parseable by `datetime.fromisoformat` (Python 3.11+: includes
`Z` and `+HH:MM` offsets).

### 1.5 Curl preamble

All examples below use these shell variables:

```bash
export AMG_BASE_URL="http://127.0.0.1:8080"
export AMG_BEARER_TOKEN="..."     # value from ~/.config/messaging-agent/.env
export AMG_AGENT_ID="claude-code"
```

---

## 2. REST API

### 2.1 `GET /messages/unread`

**Spec**: §7.4.1.

**Purpose**: Return allowlisted messages the requesting agent has not yet
acknowledged via `POST /messages/mark_read`. The cursor is per-agent: two
agents see the same envelopes until each marks read independently.

**Auth**: Bearer + `X-Agent-ID`.

**Query parameters**:

| Name | Type | Required | Default | Notes |
|------|------|----------|---------|-------|
| `source` | string enum (`imessage`, `discord`) | no | — | Filter by platform. |
| `channel_id` | string | no | — | Scope to one channel. |
| `since` | string (RFC 3339) | no | — | Only return messages with `message_ts > since`. |
| `limit` | integer | no | `20` | Max envelopes to return. Clamped to `[1, 100]` (`limit < 1` rejected with `VALIDATION_FAILED`). |

**Response** — `200 OK`:

```json
{
  "messages": [ /* Envelope, ... */ ],
  "next_since": "2026-04-25T15:32:11.123Z"
}
```

`next_since` is the maximum `message_ts` of returned envelopes; on an empty
result it echoes the canonical input `since` (or `""` if absent), so MCP
clients never have to handle a missing field.

**Error responses**: `400 AGENT_ID_REQUIRED`, `401 UNAUTHORIZED`,
`400 VALIDATION_FAILED` (bad `since` / `limit` / `source`).

**Curl**:

```bash
curl -sS "${AMG_BASE_URL}/messages/unread?limit=20&source=discord" \
  -H "Authorization: Bearer ${AMG_BEARER_TOKEN}" \
  -H "X-Agent-ID: ${AMG_AGENT_ID}"
```

---

### 2.2 `GET /messages/{message_id}`

**Spec**: §7.4.2.

**Purpose**: Fetch a single message by id. Allowlist visibility applies:
quarantined (`allowlist_status='unknown'`) rows return `404` so callers
cannot probe for their existence.

**Auth**: Bearer + `X-Agent-ID`.

**Path parameter**:

| Name | Type | Notes |
|------|------|-------|
| `message_id` | string | Must match `^msg_[0-9A-HJKMNP-TV-Z]{26}$`. |

**Response** — `200 OK` returns a full `Envelope` (see §3 below).

**Error responses**: `404 MESSAGE_NOT_FOUND` (row missing OR
`allowlist_status='unknown'`), `400 AGENT_ID_REQUIRED`, `401 UNAUTHORIZED`,
`422 VALIDATION_FAILED` (bad message-id format).

**Curl**:

```bash
curl -sS "${AMG_BASE_URL}/messages/msg_01HXYZABCDEFGHJKMNPQRSTVWX" \
  -H "Authorization: Bearer ${AMG_BEARER_TOKEN}" \
  -H "X-Agent-ID: ${AMG_AGENT_ID}"
```

---

### 2.3 `GET /messages/context`

**Spec**: §7.4.3, §5.4.

**Purpose**: Return `before` messages older than the target, the target
itself, and `after` messages newer than the target — chronologically
ordered. The target must be visible (`allowlist_status='allowed'`); the
neighbour scan additionally includes `'outbound'` rows so the agent's own
replies appear in context.

**Auth**: Bearer + `X-Agent-ID`.

**Query parameters**:

| Name | Type | Required | Default | Notes |
|------|------|----------|---------|-------|
| `channel_id` | string (min length 1) | yes | — | Channel to scan. |
| `around_message_id` | string (min length 1) | yes | — | Anchor message id. |
| `before` | integer | no | `5` | `[0, 50]`. Out-of-range → `VALIDATION_FAILED`. |
| `after` | integer | no | `5` | `[0, 50]`. Out-of-range → `VALIDATION_FAILED`. |

**Response** — `200 OK`:

```json
{ "messages": [ /* Envelope, ... */ ] }
```

**Error responses**: `404 MESSAGE_NOT_FOUND` (target missing, not visible,
or in a different channel than `channel_id`), `400 AGENT_ID_REQUIRED`,
`401 UNAUTHORIZED`, `422` (FastAPI validation envelope) when `before`/`after`
fall outside `[0, 50]`.

**Curl**:

```bash
curl -sS \
  "${AMG_BASE_URL}/messages/context?channel_id=discord:dm:1234567890&around_message_id=msg_01HXYZABCDEFGHJKMNPQRSTVWX&before=5&after=5" \
  -H "Authorization: Bearer ${AMG_BEARER_TOKEN}" \
  -H "X-Agent-ID: ${AMG_AGENT_ID}"
```

---

### 2.4 `POST /messages/mark_read`

**Spec**: §7.4.4, §5.3, §14 OQ-1.

**Purpose**: Mark message ids as read for the requesting agent. UPSERT into
`message_reads(message_id, agent_id, read_at)` with the composite PK making
the operation naturally idempotent. Per OQ-1, `marked_count` reports the
number of unique ids the client submitted, **not** the number of newly
inserted rows.

**Auth**: Bearer + `X-Agent-ID`. No `Idempotency-Key` (UPSERT).

**Request body** (`application/json`):

```json
{ "message_ids": ["msg_01HXYZ...", "msg_01HABC..."] }
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `message_ids` | array of strings | yes | May be empty (returns `marked_count=0`). Duplicates and unknown ids are silently de-duplicated / skipped at the storage layer but still counted. |

**Response** — `200 OK`:

```json
{ "marked_count": 2 }
```

**Error responses**: `400 AGENT_ID_REQUIRED`, `401 UNAUTHORIZED`,
`422 VALIDATION_FAILED` (malformed body).

**Curl**:

```bash
curl -sS -X POST "${AMG_BASE_URL}/messages/mark_read" \
  -H "Authorization: Bearer ${AMG_BEARER_TOKEN}" \
  -H "X-Agent-ID: ${AMG_AGENT_ID}" \
  -H "Content-Type: application/json" \
  -d '{"message_ids": ["msg_01HXYZABCDEFGHJKMNPQRSTVWX"]}'
```

---

### 2.5 `POST /messages/send`

**Spec**: §7.4.5, §5.2.

**Purpose**: Send an outbound message via the configured platform connector.
The route resolves `(source, channel_id)` against the `channels` table
(404 if never observed inbound), acquires one token from the per-channel
rate limiter, routes to Discord or iMessage based on the `discord:` prefix,
persists an outbound row, and returns the AMG-side message id.

**Auth**: Bearer required. `X-Agent-ID` is **not** required (sends are not
allowlist-filtered). `Idempotency-Key` is recommended; the wrapper supplies
one automatically.

**Request body** (`application/json`):

```json
{
  "channel_id": "+15551234567",
  "text": "On it.",
  "reply_to": "msg_01HABC...",
  "attachments": [
    { "url": "http://127.0.0.1:8080/attachments/att_01HABC..." }
  ]
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `channel_id` | string (min length 1) | yes | Discord (`discord:...`) or iMessage (E.164 / chat GUID). |
| `text` | string | yes | UTF-8; empty allowed for attachment-only sends. |
| `reply_to` | string (`msg_<ULID>`) or null | no | Threads under another message in the same channel. |
| `attachments` | array of `{url, path, mime?}` | no | Each entry must supply exactly one of `url` or `path`. |

**Attachment policy** (Phase 1):

- **iMessage**: `url` must be the adapter-hosted form
  (`/attachments/<id>` or `http(s)://host/attachments/<id>`); `path` must be
  absolute. Only the **first** attachment is sent (AppleScript v1 limit);
  surplus attachments are dropped with a WARN log.
- **Discord**: any attachment returns `413 ATTACHMENT_TOO_LARGE_FOR_PLATFORM`
  (passthrough is not yet implemented; the Discord upload limit is 25 MB).

**Response** — `200 OK`:

```json
{
  "message_id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",
  "sent_at": "2026-04-25T15:32:13.123Z"
}
```

If an `Idempotency-Key` is replayed with the same body hash, the cached
response body is returned verbatim (the wrapper does not surface a
distinguishing header today). A different body hash returns
`422 IDEMPOTENCY_KEY_REUSE`.

**Error responses**:

| Status | Code | When |
|--------|------|------|
| 400 | `VALIDATION_FAILED` | Bad attachment URL/path shape. |
| 401 | `UNAUTHORIZED` | Bearer missing or invalid. |
| 404 | `CHANNEL_NOT_FOUND` | `channel_id` not in `channels`. |
| 404 | `MESSAGE_NOT_FOUND` | Adapter-hosted attachment id missing or retention-deleted. |
| 413 | `ATTACHMENT_TOO_LARGE_FOR_PLATFORM` | Discord attachment in Phase 1. |
| 422 | `IDEMPOTENCY_KEY_REUSE` | Key reused with different body. |
| 429 | `RATE_LIMITED` | Per-channel rate limit exceeded; `Retry-After` set. |
| 502 | `PLATFORM_AUTH` | Platform rejected credentials (e.g. Discord 401/403). |
| 502 | `PLATFORM_SEND_FAILED` | Platform send failed after retries. |

**Curl**:

```bash
curl -sS -X POST "${AMG_BASE_URL}/messages/send" \
  -H "Authorization: Bearer ${AMG_BEARER_TOKEN}" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{
    "channel_id": "+15551234567",
    "text": "On it."
  }'
```

---

### 2.6 `GET /messages/quarantine`

**Spec**: §7.4.8, §5.7, §5.9.

**Purpose**: Operator review of inbound messages whose senders are not on
the allowlist (`allowlist_status='unknown'`). Global, not per-agent;
`X-Agent-ID` is not consulted.

**Auth**: Bearer required.

**Query parameters**:

| Name | Type | Required | Default | Notes |
|------|------|----------|---------|-------|
| `since` | string (RFC 3339) | no | — | Only return messages with `message_ts > since`. |
| `limit` | integer | no | `20` | Clamped to `[1, 100]` (`< 1` rejected). |

**Response** — `200 OK`:

```json
{
  "messages": [ /* Envelope with allowlist_status='unknown', ... */ ],
  "next_since": "2026-04-25T15:32:11.123Z"
}
```

Ordering is `message_ts DESC` (newest first), matching operator review
ergonomics.

**Error responses**: `401 UNAUTHORIZED`, `400 VALIDATION_FAILED`.

**Curl**:

```bash
curl -sS "${AMG_BASE_URL}/messages/quarantine?limit=50" \
  -H "Authorization: Bearer ${AMG_BEARER_TOKEN}"
```

---

### 2.7 `GET /attachments/{attachment_id}`

**Spec**: §7.4.9, §5.8.

**Purpose**: Stream the bytes of a re-hosted attachment from the local
store (`$AMG_ATTACHMENT_DIR/<id>`).

**Auth**: Bearer required.

**Path parameter**:

| Name | Type | Notes |
|------|------|-------|
| `attachment_id` | string | Must match `^att_[0-9A-HJKMNP-TV-Z]{26}$`. |

**Response** — `200 OK`:

- Body: raw attachment bytes.
- `Content-Type`: stored MIME from the `attachments` row.
- `Content-Length`: stored byte count.
- `Content-Disposition: inline`.

**Error responses**: `404 MESSAGE_NOT_FOUND` (row missing, retention-deleted
where `bytes_path IS NULL`, or file gone from disk), `401 UNAUTHORIZED`,
`422 VALIDATION_FAILED` (bad attachment-id format).

**Curl**:

```bash
curl -sS -OJ "${AMG_BASE_URL}/attachments/att_01HABCABCDEFGHJKMNPQRSTVWX" \
  -H "Authorization: Bearer ${AMG_BEARER_TOKEN}"
```

---

### 2.8 `GET /openapi.json`, `GET /docs`, `GET /redoc`

**Spec**: §7.4 (header table — last row).

**Purpose**: Serve the auto-generated OpenAPI 3.1 schema and the Swagger UI
/ ReDoc consumers. Re-registered behind `Depends(require_bearer)` because
FastAPI's stock routes are anonymous and the spec requires the bearer on
every endpoint.

**Auth**: Bearer required.

**Curl**:

```bash
curl -sS "${AMG_BASE_URL}/openapi.json" \
  -H "Authorization: Bearer ${AMG_BEARER_TOKEN}"
```

---

## 3. Schemas

The reference shapes for the request/response bodies above. These are the
Pydantic v2 models in `amg/core/envelope.py` and the request/response
models in the route modules.

### 3.1 Envelope (spec §7.3.1)

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

### 3.2 SendMessageRequest

```jsonc
{
  "channel_id": "+15551234567",                   // required, min length 1
  "text": "On it.",                               // required (empty allowed)
  "reply_to": "msg_01HABC...",                    // optional, msg_<ULID> or null
  "attachments": [                                // optional
    {
      "url": "http://127.0.0.1:8080/attachments/att_01HABC...",
      "path": null,                               // exactly one of url / path per item
      "mime": null                                // optional MIME hint
    }
  ]
}
```

### 3.3 MarkReadRequest / MarkReadResponse

```jsonc
// Request
{ "message_ids": ["msg_01HXYZ...", "msg_01HABC..."] }

// Response
{ "marked_count": 2 }
```

---

## 4. MCP tool surface

The four-tool MCP surface that maps 1:1 onto these REST endpoints is documented
on its own page: [MCP Tools](mcp-tools.md). The wrapper (`mcp/`, FastMCP)
injects the bearer / `X-Agent-ID` headers and an `Idempotency-Key` for
`send_message`, and returns the spec §7.4.12 error envelope verbatim.

---

## 5. Compatibility note

The mounted REST surface in `amg.app:app` is the canonical authority. The eight
routers wired by `build_app()` are the seven message/attachment routes above
plus `GET /healthz` (§7.4.10), which reports liveness, per-connector state, and
webhook-queue depth. `GET /openapi.json`, `/docs`, and `/redoc` are
re-registered behind `Depends(require_bearer)`.

One spec route is **not** yet mounted: `POST /typing` (§7.4.6). Its router
exists (`amg/api/typing.py`) but `build_app()` does not include it; it is a
best-effort typing indicator slated for a follow-on wave. Webhook outbound
delivery (§7.4.11) is a server-to-client push, not a mounted route — it is
documented in the spec and in [Operations → Webhook Receiver](../operations/webhook-receiver.md).
