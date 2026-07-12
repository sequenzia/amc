# AMG ↔ Claude Code Channels — the AMG side

**Date:** 2026-05-31
**Status:** Design note (no code changes proposed yet).
**Scope:** What the **AMG adapter** must provide / change to let a Claude Code **Channel** drive a human-attended **TUI** session through the AMG surface — both when the channel sits directly on AMG and when **Capo** brokers in the middle.

**Companion (Capo repo):** `capo/internal/docs/capo-amg-channel-bridge-design-2026-05-31.md` (the broker design + full Channels reference) and `capo/internal/docs/claude-code-interactive-session-options-2026-05-31.md` (the four-mechanism matrix). This note is the **AMG-side** counterpart: it only covers what AMG owns.

---

## TL;DR

- A Claude Code channel is **an MCP server the TUI session spawns over stdio** that pushes events into the session and (two-way) exposes a reply tool. From AMG's perspective a channel is **just another consumer of the adapter** — the push-based sibling of the existing `mcp/` wrapper. (Full Channels reference: see the Capo companion doc §1.)
- **Two topologies, two levels of AMG effort:**
  - **Capo-broker (chosen design):** the channel talks to *Capo*, and Capo talks to AMG over the **existing** webhook + REST. **AMG needs zero changes** — Capo is already an adapter consumer today.
  - **AMG-direct (`amg-channel`):** a new workspace member (sibling to `mcp/`) consumes AMG's REST + webhook directly. AMG changes are small and mostly optional.
- **The headline AMG fact:** multi-session contention is **already solved on the pull path.** ADR-0002 (per-agent read state via `message_reads` + `X-Agent-ID`) means every channel/TUI session that polls `GET /messages/unread` with its own `X-Agent-ID` gets an **independent unread queue and cursor** — no leasing, no head-of-line blocking. The *only* genuine AMG-side gap is the **single outbound webhook URL** (`AMG_WEBHOOK_URL`): the push path has exactly one consumer.

---

## 1. AMG's role in each topology

### 1.1 Capo-broker (the chosen design) — AMG unchanged

```
 iMessage/Discord ─► AMG adapter ─webhook─► Capo ──(local)──► amg-channel shim ─stdio─► TUI
                          ▲                   │
                          └─ POST /messages/send ◄── reply from TUI (via Capo)
```

AMG sees only Capo, exactly as it does today: signed webhook out, `POST /messages/send` + `POST /messages/mark_read` in. The channel, the shim, and the TUI all live Capo-side. **No AMG code, schema, or config change is required.** This is the payoff of the decoupling principle in the blueprint — the agent framework is replaceable without touching the adapter.

### 1.2 AMG-direct (`amg-channel`) — one new consumer

```
 iMessage/Discord ─► AMG adapter ─┬─webhook─► amg-channel shim ─stdio─► TUI
                                  └─ REST ◄── reply / mark_read / context
```

A new workspace member `amg-channel/` (parallel to `mcp/` and `webhook-receiver/`) bridges the AMG surface to a TUI session's stdio. AMG stays the source of truth; the channel is a thin translation layer. Useful if you want a TUI on AMG **without** Capo. Note the overlap: the research preview already ships official iMessage + Discord channels — doing it via AMG buys the unified cross-platform interface, identity linking, SQLite audit, and allowlist independence, at the cost of building/maintaining the member.

---

## 2. The AMG surface a channel consumes

All confirmed in `docs/reference/rest-api.md` and `amg/core/`.

| Channel need | AMG primitive | Notes |
|---|---|---|
| Receive new inbound messages | **`GET /messages/unread`** (pull) or the **outbound webhook** (push) | Pull returns `{ "messages": [Envelope…], "next_since": … }`; `limit` default 20, clamped `[1,100]`. Per-agent cursor via `X-Agent-ID` (ADR-0002). |
| Look up a single message | **`GET /messages/{message_id}`** | Full `Envelope`. |
| Thread context | **`GET /messages/context`** (`before`/`after`) | `{ "messages": [Envelope…] }`. Maps to the channel's "look up context" need. |
| Send a reply (reply tool) | **`POST /messages/send`** | `SendMessageRequest`; idempotency-keyed (`amg/core/idempotency.py`). |
| Mark handled | **`POST /messages/mark_read`** | UPSERTs `message_reads` keyed by `(message_id, X-Agent-ID)` — **per agent**, re-mark is a no-op. |
| Sender gating | Allowlist (ADR-0005, `amg/core/allowlist.py`) + envelope `allowlist_status` + **`GET /messages/quarantine`** | The channel can **reuse AMG's allowlist verdict** instead of reimplementing sender gating. `unread` already filters to `allowlist_status='allowed'`. |
| Attachments | **`GET /attachments/{attachment_id}`** | Re-host strategy per ADR-0003. |

**Auth / identity:** every request authenticates (rest-api §1.1) and carries a first-class **`X-Agent-ID`** header (§7.4.2). Each channel/TUI session uses its own `X-Agent-ID` — that is the seam that makes per-session unread queues work.

**Envelope:** the normalized message shape (`amg/core/envelope.py`, spec §7.3.1) is what the channel wraps into a `<channel>` tag. Map envelope fields → `meta` attributes, remembering channel `meta` keys must be `[A-Za-z0-9_]` only (hyphens silently dropped) — so `channel_id`, `sender_id`, `message_id`, `platform`, not hyphenated forms.

---

## 3. Inbound delivery: push vs pull — and why pull scales to many sessions

This is the one real AMG-side design choice.

| | **Push — outbound webhook** | **Pull — `GET /messages/unread`** |
|---|---|---|
| Mechanism | `WebhookWorker` drains `webhook_deliveries`, HMAC-signs (`X-AMG-Signature: sha256=<hex>`), POSTs to `AMG_WEBHOOK_URL`. | Channel polls with its `X-Agent-ID`; advances its own cursor via `mark_read`. |
| Latency | Low (event-driven). | Polling interval (1s is fine — local SQLite). |
| **Multi-session** | ❌ **One URL = one consumer.** Two sessions can't both be the webhook target without fan-out. | ✅ **Native.** ADR-0002 gives each `X-Agent-ID` an independent queue + cursor. N sessions = N agent IDs, no contention. |
| Config | `AMG_WEBHOOK_URL` + `AMG_WEBHOOK_SECRET` (adapter **refuses to start** if URL set without secret — OQ-6). | No new config; standard auth + `X-Agent-ID`. |
| Verify signature | Consumer checks HMAC over exact bytes. | n/a. |

**Recommendation for AMG-direct, multi-session:** use the **pull** path. It already supports many independent sessions for free and needs no AMG change. Reserve the webhook for the single-consumer broker case (Capo) or a single direct session that wants low latency.

**If push-to-many is genuinely required:** that is the only place AMG would need new work — webhook **fan-out** (multiple targets, or a per-agent subscription registry). This reopens the "multi-agent contention" open decision (blueprint §9). Recommend deferring unless a concrete need appears; the per-agent pull path covers most cases.

---

## 4. The `amg-channel/` workspace member (AMG-direct path only)

Shape it exactly like `mcp/`:

- Own `pyproject.toml`, `README.md`, `tests/` (incl. an e2e stdio test like `mcp/tests/test_e2e_stdio.py`).
- **No platform-specific code** — enforce with a `scripts/import_audit.py` clone (the `mcp/` member is statically gated this way). The channel only speaks HTTP to the adapter; iMessage/Discord knowledge stays in `amg/connectors/`.
- **stdio transport only** (consistent with ADR-0006 for the MCP wrapper; channels are stdio subprocesses by definition).
- Reuse the `mcp/` HTTP-client + error-mapping patterns (`mcp/tests/test_http_client.py`, `test_errors.py`) so the channel returns AMG's standard error envelope verbatim (rest-api §1.2).
- **Language caveat:** the channels reference is TypeScript/Bun (`@modelcontextprotocol/sdk`). The Python MCP SDK can declare `experimental_capabilities={"claude/channel": {}}`, but emitting the custom-method `notifications/claude/channel` is the one unverified bit (spike before committing to Python; otherwise a small Bun member). Either way, import-audit-clean.

---

## 5. Concrete AMG-side work items

| Path | AMG changes |
|---|---|
| **Capo-broker (chosen)** | **None.** Capo consumes the existing webhook + `POST /messages/send` + `/messages/mark_read`. AMG is already done. |
| **AMG-direct, single or per-agent-pull sessions** | Add the `amg-channel/` workspace member (consumer only). No adapter/schema change — per-agent cursors (ADR-0002) already carry it. |
| **AMG-direct, low-latency push to *multiple* sessions** | The only path that touches the adapter: webhook **fan-out** / per-agent subscription. Reopens blueprint §9 multi-agent contention. Defer unless needed. |

Optional niceties (not required): a typing-indicator passthrough if/when `/typing` (blueprint §5.1) lands; a dedicated `X-Agent-ID` naming convention for channel sessions (e.g. `cc-tui-<host>-<pid>`) so `message_reads` rows are attributable in audits.

---

## 6. Relevant ADRs, divergences, constraints

- **ADR-0002 (per-agent cursor)** — the load-bearing decision that makes multi-session channels work on pull. `message_reads(message_id, agent_id, read_at)`, composite PK; unread = `LEFT JOIN … WHERE agent_id=:id AND read_at IS NULL AND allowlist_status='allowed'`.
- **ADR-0005 (allowlist TOML)** — the channel should defer sender gating to AMG's allowlist verdict (`allowlist_status`) rather than maintaining its own; `unread` already excludes non-allowed senders, and `quarantine` exposes `unknown` ones for review.
- **ADR-0003 (attachment re-host)** — channels that surface attachments rely on this; CDN/local paths are not stable URLs.
- **ADR-0006 (MCP stdio-only)** — channels are stdio subprocesses; consistent.
- **Webhook contract** — `amg/core/webhook.py`: HMAC-SHA256 over exact bytes, `X-AMG-Signature: sha256=<hex>`, single `AMG_WEBHOOK_URL`, refuses to start without `AMG_WEBHOOK_SECRET` (OQ-6).
- **Known divergences** (`internal/notes/spec-code-divergences.md`): `VALIDATION_FAILED` vs `VALIDATION_ERROR`; DB path `state.db` vs `amg.db`. A channel surfacing AMG errors inherits the in-code spelling (`VALIDATION_FAILED`).

---

## 7. Bottom line

For the design you're actually pursuing (Capo as broker into an attended TUI), **AMG needs nothing new** — Capo is already a first-class adapter consumer, and the channel/shim/TUI all live Capo-side. If you later want a TUI **directly** on AMG, it's one consumer-only workspace member; per-agent cursors (ADR-0002) already give you independent multi-session unread queues on the pull path, and the single outbound webhook URL is the lone place the adapter itself would need work — and only if you require low-latency push to *multiple* sessions at once.

---

## 8. Sources

- AMG REST surface: `docs/reference/rest-api.md` (§2 endpoints, §3 schemas, §1 conventions).
- AMG webhook: `amg/core/webhook.py`; idempotency: `amg/core/idempotency.py`.
- ADRs: `internal/adrs/0002-per-agent-cursor.md`, `0003-attachment-rehost.md`, `0005-allowlist-toml.md`, `0006-mcp-stdio-only.md`.
- MCP wrapper sibling pattern: `mcp/` (`pyproject.toml`, `scripts/import_audit.py`, `tests/test_e2e_stdio.py`).
- Channels reference: <https://code.claude.com/docs/en/channels-reference>; official members: <https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins>.
- Capo-side companion: `capo/internal/docs/capo-amg-channel-bridge-design-2026-05-31.md`.
