# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository State

**Pre-implementation.** As of the last update to this file, the repo contains only a README, the architectural blueprint at `internal/blueprints/agent-messaging-channel.md`, and Claude Code config. No source code, build system, or tests exist yet. There are therefore no build/lint/test commands to document — add them here when the first runtime is scaffolded.

When the user asks to start implementing, the blueprint is the source of truth. Read it in full before proposing structure or naming.

## Project: Agent Messaging Channel (AMC)

AMC is a single-Mac service that lets one AI agent send and receive messages on **iMessage** and **Discord** through a unified interface. The design priority is decoupling: the agent framework, the transport, and the platform connectors must each be replaceable without rewriting the others.

## Architecture (from the blueprint)

Three layers, each independently replaceable:

```
Agent  ──MCP──►  MCP Wrapper  ──HTTP──►  Adapter HTTP API  ──►  Connectors  ──►  iMessage / Discord
   │                                          ▲
   └──────────── Direct HTTP ─────────────────┘
                                              │
                                              ▼
                                          SQLite
```

* **Adapter HTTP API** — the source of truth. A single process (FastAPI or Hono) that runs both connectors as background tasks, persists to SQLite, and exposes REST endpoints plus an outbound webhook for new messages. Agents that don't speak MCP hit this directly.
* **MCP Wrapper** — a thin TypeScript layer using `@modelcontextprotocol/sdk` that translates four tools (`list_unread_messages`, `send_message`, `mark_read`, `get_message_context`) into HTTP calls against the adapter. It must contain **no platform-specific code**.
* **Connectors** — one per platform. iMessage polls `~/Library/Messages/chat.db` and sends via AppleScript; Discord uses a Gateway WebSocket plus REST.

### Critical Contracts

These shapes are what makes the layering work. Don't change them without updating the blueprint and asking the user.

* **Normalized message envelope** (blueprint §3) — every message in the system, inbound or outbound, conforms to this JSON shape. Adding a new platform means writing one connector that produces this envelope; nothing else changes.
* **MCP tool surface** (blueprint §6.1) — exactly four tools, mirroring how a human assistant works: see what's new, look up context, reply, mark done. New capabilities should be additive tools, not modifications to these four.
* **Adapter REST endpoints** (blueprint §5.1) — `/messages/unread`, `/messages/{id}`, `/messages/context`, `/messages/mark_read`, `/messages/send`, `/typing`, plus an outbound webhook.

### Storage

SQLite, four tables: `messages`, `channels`, `senders`, `identity_links` (blueprint §5.3). The `identity_links` table is what eventually maps the same human across iMessage and Discord — keep this in mind when modeling sender IDs.

## macOS-Specific Constraints (iMessage path)

These will bite a future implementer:

* The adapter process needs **Full Disk Access** granted in System Settings to read `chat.db`.
* First outbound message triggers an **Automation permission** prompt for Messages; the AppleScript send will silently fail until accepted.
* The Mac must stay awake (`caffeinate -dimsu` or Energy settings) or the connector stops processing.
* Track the last processed `ROWID` in connector state so polling survives restarts. Polling at 1s is fine — `chat.db` is local SQLite.

## Implementation Phases (planned order)

Per blueprint §8, work proceeds Discord-first because it's exercisable end-to-end with a bot token and no macOS permission dance:

1. Adapter skeleton + storage + Discord connector. Validate with `curl`.
2. iMessage connector (lift from `anthropics/claude-plugins-official/tree/main/external_plugins/imessage`, strip the MCP scaffolding, adapt to the envelope).
3. MCP wrapper — four thin HTTP calls. Verify with the MCP Inspector.
4. Hardening: webhook retries, send rate limiting, attachment re-hosting, identity linking, observability.

## Open Decisions

The blueprint intentionally leaves these for the user to call:

* **Adapter language**: Python + FastAPI vs. TypeScript + Hono. Per the global CLAUDE.md, ask before picking. The MCP wrapper is fixed at TypeScript regardless.
* **Group chat support on iMessage** in v1.
* **Attachment strategy**: pass-through vs. adapter-rehosted (Discord CDN URLs expire; iMessage attachments are local file paths — neither is a stable URL the agent can hand back).
* **Multi-agent contention** on a single adapter (per-agent cursors vs. leasing). Single-agent is the v1 default.

See blueprint §9 for the full list.

## Reference Code (external)

* iMessage connector starting point — `https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/imessage` (lift platform code, drop the MCP scaffolding)
* MCP TypeScript SDK — `https://github.com/modelcontextprotocol/typescript-sdk`
* `discord.js` — recommended over the Anthropic Discord plugin for the connector

## Working Conventions for This Repo

* When the blueprint and an in-flight discussion conflict, surface the conflict and ask. Don't silently diverge from the blueprint — update it explicitly so future sessions stay aligned.
* Architectural decisions (language pick, persistence engine, package manager, deployment shape) need confirmation before code lands.
* Update this file as soon as the first runtime is scaffolded — replace the "Pre-implementation" section with real build/test/run commands.
