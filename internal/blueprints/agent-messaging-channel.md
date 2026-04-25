# Agent Messaging Channel (AMC) Blueprint

A hybrid architecture for an AI agent that sends and receives messages on iMessage and Discord, designed for a single Mac host with portability across agent frameworks.

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
    "display_name": "Alice"
  },
  "text": "hey, can you check the build?",
  "attachments": [
    { "type": "image", "url": "...", "mime": "image/png" }
  ],
  "reply_to": "msg_01HABC..." | null,
  "timestamp": "2026-04-25T15:32:11Z",
  "raw": { ... platform-native object for debugging ... }
}
```

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

### 5.2 Authentication

A static bearer token in `Authorization: Bearer ...` for all endpoints. Set in an env var, generated once. Since the API binds to localhost by default, this is mostly defense in depth.

### 5.3 Storage Schema

SQLite tables:

* `messages` (id, source, channel_id, channel_type, sender_id, sender_display_name, text, attachments_json, reply_to, timestamp, raw_json, read_at)
* `channels` (channel_id, source, channel_type, last_seen_message_id, metadata_json)
* `senders` (sender_id, source, display_name, allowlist_status, first_seen, last_seen)
* `identity_links` (claimed_user_id, source, sender_id) for mapping the same human across platforms

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

### 6.3 Optional Notification Stream

For MCP clients that subscribe to server notifications, the wrapper can also emit `notifications/messages/new` events when the adapter receives a message. This is optional and additive; agents that ignore notifications fall back to polling `list_unread_messages` and behave identically.

## 7. Deployment on a Single Mac

Three processes managed by `launchd` (or `pm2` for simplicity during development):

1. **Adapter HTTP service** on `localhost:8080`. Long-running, restarts on crash.
2. **MCP wrapper** spawned per agent session via stdio (Claude Desktop, Claude Code) or run as a long-lived HTTP MCP server on `localhost:8081` (for remote agents).
3. **Agent runtime** wherever it lives; could be Claude Desktop, a Python script, or a hosted worker that connects via the MCP wrapper's HTTP transport.

A single `launchd` plist for the adapter is enough for personal use. Logs to `~/Library/Logs/messaging-agent/`. Secrets (Discord bot token, bearer token) in `~/.config/messaging-agent/.env` with mode 600.

### 7.1 macOS-Specific Setup Checklist

* Grant Full Disk Access to the Terminal or process binary running the adapter.
* On first AppleScript send, accept the Automation prompt for Messages.
* Use a dedicated Apple ID for iMessage if this is more than personal experimentation.
* Keep the Mac awake: `caffeinate -dimsu` or "Prevent automatic sleeping" in Energy settings.

## 8. Implementation Phases

**Phase 1: Adapter and Discord only (1-2 days)**
Build the HTTP API skeleton, storage schema, and Discord connector. Validate end-to-end with `curl`. No MCP yet.

**Phase 2: iMessage connector (1-2 days)**
Lift code from the Claude Code plugin, adapt to the normalized envelope, wire into the same adapter. Test the AppleScript send path and Full Disk Access flow.

**Phase 3: MCP wrapper (half a day)**
Four tools, each a thin HTTP call. Verify with the MCP Inspector and one real agent.

**Phase 4: Hardening (ongoing)**
Webhook delivery with retries, rate limiting on `send_message`, attachment download and re-host, identity linking, observability.

## 9. Open Questions for Later

* **Group chat semantics on iMessage.** The chat.db schema for groups is messier than DMs; decide whether to support them in v1 or punt.
* **Attachment handling.** iMessage attachments live on disk at known paths; Discord attachments are CDN URLs that expire. The normalized envelope should probably ship a stable URL (re-hosted by the adapter) rather than passing through.
* **Multi-agent contention.** If two agent processes both poll `list_unread_messages` against the same adapter, you need either per-agent cursors or a leasing model. Single-agent for v1 is simpler.
* **Read receipts and tapbacks.** iMessage supports both; Discord has reactions. Worth exposing as separate tools (`react`, `mark_delivered`) once the basics work.
* **Permission prompts.** If the agent wants to do something sensitive (send to a new contact, post in a public Discord channel), the adapter could hold the message and require explicit approval. The Claude Code channels protocol's permission relay is the inspiration here, but it can be done over plain HTTP with a webhook to a human-facing approver.

## 10. Reference Code

* iMessage connector starting point: `https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/imessage`
* Discord channel plugin (reference, not a dependency): `https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/discord`
* MCP TypeScript SDK: `https://github.com/modelcontextprotocol/typescript-sdk`
* `discord.js` (recommended for the Discord connector): `https://discord.js.org/`
