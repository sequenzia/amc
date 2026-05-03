# Agent Messaging Channel (AMC) Blueprint

A hybrid architecture for an AI agent that sends and receives messages on iMessage and Discord, designed for a single Mac host with portability across agent frameworks.

> **Source of truth for v1**: this blueprint is the **architectural** source of truth — the layering, the envelope concept, the connector model, the persistence shape. For the **v1 implementation contract** (exact envelope field types and constraints, complete REST endpoint list with request / response shapes, full SQL schema with field types and indexes, stable error codes, env-var defaults, phase-by-phase acceptance gates), the source of truth is `specs/agent-messaging-channel-SPEC.md` v1.1. Where the blueprint summarizes and the spec specifies, the spec wins for v1. Where they differ at the architectural level (e.g. blueprint §3 envelope shape vs spec §7.3.1, or blueprint §5.3 schema vs spec §7.3.2/3), the blueprint has been reconciled to match the spec — see the Phase 1 reconciliation footnote at the end of this document.

## 1. Goals

* Receive and respond to messages on iMessage and Discord through a single agent.
* Decouple platform integrations from agent logic so the agent framework can be swapped without rewriting connectors.
* Expose the same capabilities over both a plain HTTP API and an MCP server, so MCP-aware agents (Claude, OpenAI Agents SDK, Mastra, LangChain) and non-MCP clients (scripts, webhooks, automation tools) can both use it.
* Run entirely on one Mac, with no external hosting dependencies for the iMessage path.

## 2. Architecture Overview

Three layers, each replaceable independently:

```
+------------------------------------------------------------+
|  Agent (Claude SDK, OpenAI Agents, custom loop, etc.)      |
+------------------------------------------------------------+
                  |                          |
                  | MCP (stdio/HTTP)         | Direct HTTP
                  v                          v
+------------------------------------------------------------+
|  MCP Wrapper (thin)        |     (skips MCP wrapper)       |
+------------------------------------------------------------+
                  |
                  v
+------------------------------------------------------------+
|  Adapter HTTP API (FastAPI / Hono / Express)               |
|  - Normalized message envelope                             |
|  - Webhook outbound for new messages                       |
|  - REST endpoints for send / list / mark-read              |
+------------------------------------------------------------+
        |                                |
        v                                v
+----------------------+      +-----------------------------+
|  iMessage Connector  |      |  Discord Connector          |
|  - chat.db watcher   |      |  - Gateway WebSocket        |
|  - AppleScript send  |      |  - REST send                |
+----------------------+      +-----------------------------+
                  |
                  v
+------------------------------------------------------------+
|  Storage (SQLite)                                          |
|  - Inbox queue, conversation history, identity mapping     |
+------------------------------------------------------------+
```

The adapter is the source of truth. The MCP wrapper is a thin translation layer that calls the adapter's HTTP API and exposes its functions as MCP tools.

## 3. Normalized Message Envelope

Every message in the system, regardless of platform, conforms to this shape:

```json
{
  "id": "msg_01HXYZ...",
  "source": "imessage" | "discord",
  "channel_id": "+15551234567" | "discord:1234567890",
  "channel_type": "dm" | "group",
  "sender": {
    "id": "+15551234567" | "discord:user:123",
    "display_name": "Alice",
    "person_id": "alice" | null
  },
  "text": "hey, can you check the build?",
  "attachments": [
    {
      "id": "att_01HABC...",
      "url": "http://127.0.0.1:8080/attachments/att_01HABC...",
      "mime": "image/png",
      "size_bytes": 124356
    }
  ],
  "reply_to": "msg_01HABC..." | null,
  "timestamp": "2026-04-25T15:32:11Z",
  "direction": "inbound" | "outbound",
  "raw": { ... platform-native object for debugging ... }
}
```

Field notes (post-Phase 1, reconciled with spec §7.3.1):

* `direction`: `"inbound"` for messages received from a platform, `"outbound"` for messages the agent sent through AMC. Required on every envelope.
* `sender.person_id`: optional identity-link key. Present when the sender is allowlisted with a `person_id` that maps the same human across iMessage and Discord; otherwise `null`.
* `attachments[].id`: ULID with `att_` prefix, assigned by the adapter at re-host time.
* `attachments[].size_bytes`: integer byte count of the re-hosted file.
* `attachments[].url`: **always adapter-hosted on inbound** (`http://<bind>/attachments/{id}`). The adapter pulls Discord CDN URLs and iMessage filesystem paths into local storage and serves them back at a stable, authenticated URL. Outbound `attachments[]` (in `POST /messages/send`) accept the original URL or path; the adapter re-hosts before delivery.

This envelope is what the adapter HTTP API returns, what the MCP wrapper relays, and what the agent reasons about. Adding a third platform (Slack, SMS, Telegram) means writing one new connector that produces this shape.

## 4. Platform Connectors

### 4.1 iMessage Connector

**Source material:** Lift the database polling and AppleScript send logic from `anthropics/claude-plugins-official/tree/main/external_plugins/imessage`. Strip the MCP server scaffolding; keep the platform code.

**Responsibilities:**
* Watch `~/Library/Messages/chat.db` (SQLite) for new rows in the `message` table since the last seen `ROWID`.
* Translate raw rows into the normalized envelope, resolving handles to phone numbers or Apple ID emails.
* Send outbound messages by invoking AppleScript (`osascript`) against the Messages app.
* Maintain a sender allowlist; messages from non-allowlisted handles are stored but flagged for review, not forwarded to the agent.

**Requirements:**
* macOS host with Messages signed in.
* Full Disk Access granted to the process running the connector (for `chat.db` reads).
* Automation permission granted for AppleScript control of Messages (prompted on first send).

**Polling strategy:** 1 second interval is fine. The chat.db is a local SQLite file, so reads are cheap. Track the last processed `ROWID` in the connector's own state to survive restarts.

### 4.2 Discord Connector

**Source material:** The Discord channel plugin in the same Anthropic repo is a good reference, but `discord.js` or `discord.py` directly is cleaner since you do not need the channel protocol shape.

**Responsibilities:**
* Maintain a persistent WebSocket gateway connection to Discord.
* On `MESSAGE_CREATE`, normalize and enqueue.
* Send outbound messages via the Discord REST API.
* Respect the Message Content intent (must be enabled in the Developer Portal).

**Requirements:**
* Bot token (Developer Portal).
* Bot invited to relevant servers, or operating in DMs.
* Message Content intent enabled.

## 5. Adapter HTTP API

A single FastAPI (Python) or Hono (TypeScript) process that runs both connectors as background tasks and exposes a unified REST surface. SQLite handles persistence.

### 5.1 Endpoints

**Inbound (agent reads from these):**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/messages/unread` | List unread normalized messages, optionally filtered by `source`, `channel_id`, `since`, `limit` |
| GET | `/messages/{id}` | Fetch a single message by ID |
| GET | `/messages/context` | Fetch N messages around a target message ID (for thread context) |
| POST | `/messages/mark_read` | Mark one or more message IDs as read |

**Outbound (agent writes through these):**

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/messages/send` | Send a message to a channel; supports `reply_to` for threading |
| POST | `/typing` | Optional: emit a typing indicator |

**Webhook (push to subscribers):**

| Method | Path | Purpose |
|--------|------|---------|
| POST | `{configured_webhook_url}` | Adapter POSTs the normalized envelope to a configured URL on every new message |

The webhook is what makes this useful for non-polling consumers (n8n, Zapier, custom event-driven agents). The MCP wrapper does not use it; it polls the unread endpoint instead.

**Operator and system endpoints** (added during Phase 4 reconciliation; see spec §7.4.8–§7.4.10 for full request / response shapes):

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/messages/quarantine` | Operator review of non-allowlisted inbound messages (bearer-only; no `X-Agent-ID`) |
| GET | `/attachments/{id}` | Serve re-hosted attachment bytes (bearer-only) |
| GET | `/healthz` | Liveness + connector states + queue depths |
| GET | `/openapi.json`, `GET /docs` | OpenAPI 3.1 schema + interactive docs (bearer-required) |

### 5.2 Authentication

A static bearer token in `Authorization: Bearer ...` for all endpoints. Set in an env var, generated once. Since the API binds to localhost by default, this is mostly defense in depth.

### 5.3 Storage Schema

SQLite tables (post-Phase 1, reconciled with spec §7.3.2/§7.3.3):

* `messages` (id, source, channel_id, channel_type, sender_id, text, reply_to, direction, allowlist_status, message_ts, created_at, attachments_json, raw_json) — note: **no `read_at` column**; per-agent read state lives in `message_reads`. Spec §7.3.3 also adds `webhook_deliveries`, `idempotency_keys`, and `connector_state` tables that the adapter owns.
* `message_reads` (message_id, agent_id, read_at) — composite-PK join table. Each `(message_id, agent_id)` row records that a specific agent has marked that specific message read. This is what makes per-agent cursors work: the adapter's "unread" view for `X-Agent-ID = A` is `messages LEFT JOIN message_reads ON message_id WHERE agent_id = A AND read_at IS NULL`.
* `channels` (source, channel_id, channel_type, last_seen_message_id, metadata_json) — composite PK `(source, channel_id)`.
* `senders` (source, sender_id, display_name, allowlist_status, person_id, first_seen, last_seen) — composite PK `(source, sender_id)`; `person_id` is the optional identity-link key.
* `identity_links` (person_id, source, sender_id) for mapping the same human across platforms.
* `attachments` (id, message_id, mime, size_bytes, bytes_path, original_url_or_path, created_at) — adapter-owned re-host store.

**Per-agent cursor rationale**: The blueprint originally carried `read_at` as a column on `messages`, which implicitly assumed one consumer. Two agents polling `list_unread_messages` against the same adapter would race on that single column — whichever agent called `mark_read` first would silently mark the message read for the other agent too. The `message_reads` join table makes "read" a relation between an agent and a message rather than a property of the message, so each agent has an independent cursor and `mark_read` only affects the calling agent's view. Phase 1 closes the "multi-agent contention" item from §9 in this direction (per-agent cursors, no leasing).

## 6. MCP Wrapper

A thin TypeScript MCP server (using `@modelcontextprotocol/sdk`) that imports nothing platform-specific. It only knows how to make HTTP calls to the adapter.

### 6.1 Tool Surface

These are the four core tools, each backed by one HTTP call:

#### `list_unread_messages`

Returns messages the agent has not yet processed.

```
Input:
  since:       ISO 8601 timestamp (optional, default: last poll)
  source:      "imessage" | "discord" | null (optional)
  channel_id:  string (optional, scope to one conversation)
  limit:       integer (default 20, max 100)

Output:
  messages: [normalized envelope, ...]
  next_since: ISO 8601 timestamp to pass on the next call
```

#### `send_message`

Sends a reply.

```
Input:
  channel_id:  string (required)
  text:        string (required)
  reply_to:    string (optional, message ID to thread under)
  attachments: [{ url, mime }] (optional)

Output:
  message_id:  string
  sent_at:     ISO 8601 timestamp
```

#### `mark_read`

Acknowledges messages so they do not appear in subsequent `list_unread_messages` calls.

```
Input:
  message_ids: [string, ...] (required)

Output:
  marked_count: integer
```

#### `get_message_context`

Returns surrounding messages for a target message, so the agent can reason about a thread without dumping the whole channel into context.

```
Input:
  channel_id:        string (required)
  around_message_id: string (required)
  before:            integer (default 5)
  after:             integer (default 5)

Output:
  messages: [normalized envelope, ...] in chronological order
```

### 6.2 Why These Four

The split mirrors how a human assistant works: see what is new, look up surrounding context if needed, write a reply, mark the task done. Anything fancier (search, summarization, channel listing) can be added as additional tools without changing this core. Keeping the surface small means new agents can be productive against it on day one.

**v1 confirmation (post-Phase 1)**: the v1 tool surface is exactly these four — `list_unread_messages`, `send_message`, `mark_read`, `get_message_context`. They map 1:1 to the v1 REST endpoints listed in §5.1 (`GET /messages/unread`, `POST /messages/send`, `POST /messages/mark_read`, `GET /messages/context`). No additions land in v1; reconciled with spec §7.4.7.

### 6.3 Optional Notification Stream

For MCP clients that subscribe to server notifications, the wrapper can also emit `notifications/messages/new` events when the adapter receives a message. This is optional and additive; agents that ignore notifications fall back to polling `list_unread_messages` and behave identically.

### 6.4 Future Work (post-v1 tools)

These tools are deliberately **out of scope for v1** but follow the same additive pattern — each is a new tool backed by a new adapter endpoint, no changes to the existing four:

* `react(message_id, emoji)` — add a reaction to a message. Maps to Discord reactions and iMessage tapbacks (six fixed reactions).
* `unreact(message_id, emoji)` — remove a reaction the agent previously added.
* `edit_message(message_id, text)` — edit a previously-sent outbound message. Discord supports this natively; iMessage support depends on macOS version (15+ allows edits within a 15-minute window).
* `delete_message(message_id)` — delete a previously-sent outbound message. Same platform-version caveat for iMessage.
* `fetch_thread(channel_id, root_message_id)` — fetch a full Discord thread by root message; iMessage has no native thread concept so this is Discord-only.

Adding any of these is purely additive: the existing four tools, the envelope, and the storage schema do not change. See spec §15.3 for the post-Phase 1 reconciliation that pinned this list.

## 7. Deployment on a Single Mac

Two processes managed by `launchd` (or `pm2` for simplicity during development):

1. **Adapter HTTP service** on `localhost:8080`. Long-running, restarts on crash.
2. **MCP wrapper** spawned per agent session via **stdio** (Claude Desktop, Claude Code, Codex CLI, Codex Desktop). The wrapper is a stdio child of each agent process; there is no long-lived MCP server in v1.
3. **Agent runtime** wherever it lives; in v1 this is a stdio-MCP-speaking client (Claude Code, Claude Desktop, Codex CLI, Codex Desktop) or a non-MCP script that calls the adapter directly over HTTP.

**v1 confirmation (post-Phase 1)**: MCP wrapper ships **stdio-only**. Reconciled with spec §8.1 / §8.2 (HTTP MCP transport listed as out-of-scope for v1) and §7.4.7. The HTTP MCP transport ("long-lived MCP server on `localhost:8081`") is **future work**, not v1 — see §9 below.

A single `launchd` plist for the adapter is enough for personal use. Logs to `~/Library/Logs/messaging-agent/`. Secrets (Discord bot token, bearer token) in `~/.config/messaging-agent/.env` with mode 600.

### 7.1 macOS-Specific Setup Checklist

* Grant Full Disk Access to the Terminal or process binary running the adapter.
* On first AppleScript send, accept the Automation prompt for Messages.
* Use a dedicated Apple ID for iMessage if this is more than personal experimentation.
* Keep the Mac awake: `caffeinate -dimsu` or "Prevent automatic sleeping" in Energy settings.

### 7.2 Future Work (post-v1 transports)

* **HTTP MCP transport** — long-lived MCP server bound to `localhost:8081` (or remote) for agents that cannot or do not want to spawn a stdio child. All four named v1 clients (Claude Code, Claude Desktop, Codex CLI, Codex Desktop) support stdio, so v1 ships without this. Add when a remote-agent use case appears.

## 8. Implementation Phases

**Phase 0: Test fixtures and platform stubs**
Build the fixture `chat.db`, `FakeAppleScriptSender` shim, and in-process fake Discord gateway + REST. All later phases depend on these so that build-time acceptance never requires Full Disk Access, Automation, a real Apple ID, or a real Discord bot token. Added in spec v1.1's autonomous-build-acceptance reframing.

**Phase 1: Adapter and Discord only**
Build the HTTP API skeleton, storage schema, and Discord connector. Validate end-to-end against the Phase 0 fakes (no real Discord credentials).

**Phase 2: iMessage connector**
Lift code from the Claude Code plugin, adapt to the normalized envelope, wire into the same adapter. Test the AppleScript send path against `FakeAppleScriptSender` and the chat.db read path against the fixture DB. Operator-side FDA + Automation flow is documented in `SETUP.md` for deployment time, not exercised at build time.

**Phase 3: MCP wrapper**
Four tools, each a thin HTTP call. Verify with an automated `@modelcontextprotocol/sdk` client harness; MCP Inspector and a live agent host are operator activities, not build-acceptance gates.

**Phase 4: Hardening**
Webhook delivery with retries, rate limiting on `send_message`, attachment download and re-host, identity linking, observability. Acceptance includes a bounded automated stability run (≤ 60 minutes of synthetic traffic against the Phase 0 fakes) plus a crash-and-relaunch test.

> Per-phase deliverables, completion criteria, and checkpoint gates live in spec §9.0–§9.4. The list above is the architectural sketch only; it is intentionally short so that the spec can evolve the gate definitions without churning the blueprint.

## 9. Open Questions for Later

Resolved during Phase 1 (struck through; kept here for traceability):

* ~~**Group chat semantics on iMessage.** The chat.db schema for groups is messier than DMs; decide whether to support them in v1 or punt.~~ **Resolved (Phase 1): deferred.** v1 returns `channel_type = "dm"` only for iMessage. iMessage group support is post-v1. See spec §8.2.
* ~~**Attachment handling.** iMessage attachments live on disk at known paths; Discord attachments are CDN URLs that expire. The normalized envelope should probably ship a stable URL (re-hosted by the adapter) rather than passing through.~~ **Resolved (Phase 1): adapter re-hosts.** Inbound attachments are pulled into local storage (`AMC_ATTACHMENT_DIR`) and exposed at a stable, bearer-authed `GET /attachments/{id}` URL. Outbound `attachments[]` accept the original URL or path; the adapter re-hosts before delivery. See §3 (envelope) and spec REQ-AMC-008.
* ~~**Multi-agent contention.** If two agent processes both poll `list_unread_messages` against the same adapter, you need either per-agent cursors or a leasing model. Single-agent for v1 is simpler.~~ **Resolved (Phase 1): per-agent cursors.** Read state lives in the `message_reads(message_id, agent_id, read_at)` join table keyed by the `X-Agent-ID` header. Each agent has an independent unread view; `mark_read` only affects the calling agent. See §5.3.

Still open:

* **Read receipts and tapbacks.** iMessage supports both; Discord has reactions. Worth exposing as separate tools (`react`, `unreact`, `mark_delivered`) once the basics work. Listed under §6.4 Future Work.
* **Permission prompts.** If the agent wants to do something sensitive (send to a new contact, post in a public Discord channel), the adapter could hold the message and require explicit approval. The Claude Code channels protocol's permission relay is the inspiration here, but it can be done over plain HTTP with a webhook to a human-facing approver. Not in v1; design TBD.

> The blueprint's open questions are **product-shape** questions ("will we ever build group chat? will tapbacks ever be a tool?"). For **v1 implementation-choice** open questions (e.g. `marked_count` semantics, idempotency-key collision resolution, allowlist reload behavior), see spec §14 (OQ-1 through OQ-7). Resolved OQs are tracked under `internal/notes/` (e.g. `oq-1-decision.md`).

## 10. Reference Code

* iMessage connector starting point: `https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/imessage`
* Discord channel plugin (reference, not a dependency): `https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/discord`
* MCP TypeScript SDK: `https://github.com/modelcontextprotocol/typescript-sdk`
* `discord.js` (recommended for the Discord connector): `https://discord.js.org/`

---

*Reconciled at v1 acceptance, against `specs/agent-messaging-channel-SPEC.md` v1.1 and the Phase 4 stability run.*

### Phase 1 reconciliation (per spec §15.3)

The end-of-Phase-1 pass updated:

* §3 Envelope — added `direction`, `sender.person_id`, `attachments[].id`, `attachments[].size_bytes`; clarified that `attachments[].url` is always adapter-hosted on inbound.
* §5.3 Storage — replaced the `read_at` column on `messages` with the `message_reads(message_id, agent_id, read_at)` join table; added `attachments` table; documented per-agent cursor rationale.
* §6.2 — confirmed v1 tool surface is exactly the original four (`list_unread_messages`, `send_message`, `mark_read`, `get_message_context`).
* §6.4 — added Future Work subsection listing `react`, `unreact`, `edit_message`, `delete_message`, `fetch_thread`.
* §7 — confirmed v1 ships **stdio MCP only**; HTTP MCP transport moved to §7.2 Future Work.
* §9 — struck through three resolved items (group chat = deferred, attachment strategy = re-host, multi-agent contention = per-agent cursors); permission-gating and tapbacks remain open.

### Phase 4 reconciliation (this pass)

The end-of-Phase-4 pass added:

* **Intro** — added a "Source of truth for v1" callout pointing readers at `specs/agent-messaging-channel-SPEC.md` v1.1 for v1 implementation contracts.
* §5.1 — added the Operator and system endpoints subgroup (`/messages/quarantine`, `/attachments/{id}`, `/healthz`, `/openapi.json`) so the blueprint's REST inventory matches what `amc/api/` actually serves and what spec §7.4 specifies.
* §8 — inserted Phase 0 (test fixtures + platform stubs) at the top of the phase list and added a pointer that completion criteria live in spec §9.
* §9 — added a pointer to spec §14 for v1 implementation-choice open questions (OQ-1 through OQ-7).
* Footer (this block) — refreshed the reconciliation note to reflect the v1 acceptance scope.

The full drift walk for this pass is recorded in `internal/notes/blueprint-drift.md` (B-1 through B-6). The blueprint contains **no mermaid diagrams** in v1; the spec's mermaid diagrams (§4.2, §4.3, §7.1, §7.3.2, §7.5) were verified against the as-built data flow during this pass and are consistent.

### Spec ↔ code drift carried over from Phase 1

These remain open and require a user decision (see `internal/notes/spec-code-divergences.md`):

* `VALIDATION_FAILED` (in code, `amc/core/errors.py`) vs `VALIDATION_ERROR` (in spec §7.4.12). Treat `VALIDATION_FAILED` as canonical pending user decision.
* Default DB filename `state.db` (in code, `amc/core/db.py` and `amc/migrations/env.py`) vs `amc.db` (in spec §11.2 `AMC_DB_PATH` row). Treat `state.db` as canonical pending user decision. Env var name `AMC_DB_PATH` is identical in both surfaces.

Neither item blocks v1 acceptance — both are name / default-value drift, not behavioural drift.
