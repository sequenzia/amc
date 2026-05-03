# Spec Analysis Report: Agent Messaging Channel (AMC)

**Analyzed**: 2026-05-02 12:00
**Reviewed**: 2026-05-02 (HTML interactive review session)
**Spec Path**: `/Users/sequenzia/dev/repos/amc/specs/agent-messaging-channel-SPEC.md`
**Detected Depth Level**: Full-Tech
**Status**: Reviewed — 14 of 20 findings resolved, 6 pending (all suggestions)

---

## Summary

| Category | Critical | Warning | Suggestion | Total |
|----------|----------|---------|------------|-------|
| Inconsistencies | 0 | 6 | 2 | 8 |
| Missing Information | 0 | 4 | 2 | 6 |
| Ambiguities | 0 | 3 | 1 | 4 |
| Structure Issues | 0 | 1 | 1 | 2 |
| **Total** | **0** | **14** | **6** | **20** |

### Overall Assessment

This is a high-quality Full-Tech spec. The author has explicitly tracked divergences from the blueprint (§7.3.1, §15.3) and deferred opens (§14 OQ-1..7), which removes a large class of would-be findings from the analysis. The 20 real findings are concentrated in three areas: schema/API surface inconsistencies (sender and channel ID namespacing, retry-count phrasing, endpoint auth-header documentation), incomplete data-model field definitions, and a few naming/structural nits. None are critical, but FIND-001 through FIND-006 will compound into implementation bugs if not nailed down before Phase 1.

---

## Resolution Summary (2026-05-02)

The HTML interactive review session resolved 12 of the 20 findings (all warning-severity). Two follow-up edits resolved FIND-005 (the `404 CHANNEL_NOT_FOUND` gap on `POST /messages/send`) and FIND-009 (the SIGHUP allowlist-reload mechanism clarification). **Final count: 14 resolved, 6 pending — all 14 warning-severity findings are now closed; the 6 remaining are suggestions only.**

### Resolved (14)

| # | Title | Disposition |
|---|-------|-------------|
| FIND-001 | iMessage IDs not consistently namespaced | Option (b): kept iMessage IDs raw, dropped "platform-namespaced" wording, composite PKs (see FIND-010) |
| FIND-002 | `senders.allowlist_status` enum undefined | Added `senders` field-def table to §7.3.3; clarified messages enum as denormalized snapshot |
| FIND-003 | iMessage send retry off-by-one | Standardized to "up to 4 total attempts (1 initial + 3 retries)" across §5.2 and §6.4 |
| FIND-004 | §7.4.2 uses `404 NOT_FOUND` | Now `404 with code=MESSAGE_NOT_FOUND`; cross-references §7.4.12 |
| FIND-005 | `POST /messages/send` missing `404 CHANNEL_NOT_FOUND` response | Added the missing response line to §7.4.5; reordered 4xx/5xx codes ascending |
| FIND-006 | Per-endpoint X-Agent-ID inconsistent | Added Authentication line to all §7.4.x; added header-requirements summary table |
| FIND-007 | Idempotency-Key on `mark_read` | Option (a): dropped from §7.4.4 example; UPSERT semantics noted |
| FIND-008 | Discord "REST replay if supported" | Option (a): RESUME-only with soft gap; REST-poll deferred post-v1 |
| FIND-009 | SIGHUP allowlist reload mechanism unspecified | Appended one-shot-resolution sentence to §5.7; cross-referenced OQ-4 |
| FIND-010 | Single-column PKs on `channels`/`senders` | Composite PKs `(source, channel_id)` and `(source, sender_id)` |
| FIND-011 | Missing field-def tables | Option (a): added 5 tables (`channels`, `senders`, `identity_links`, `attachments`, `connector_state`) to §7.3.3 |
| FIND-012 | Glossary missing ULID/HMAC/chat.guid/RFC 3339 | All 4 terms added to §15.1 |
| FIND-013 | §6.2 Authorization implies enforcement | Option (a): renamed heading to "Authorization (Conventional Use — not enforced in v1)" |
| FIND-014 | §9.0 "Pre-Phase 0" misleading | Renamed to "Phase 0 — Spike POC" |

### Pending (6 — all suggestions)

| # | Severity | Title | Notes |
|---|----------|-------|-------|
| FIND-015 | Suggestion | §3.2 "Setup repeatability" is n=1, not a metric | |
| FIND-016 | Suggestion | §7.4.2 lacks response example | Partially addressed by FIND-004 fix (cross-ref to §7.4.12); a JSON example would still help |
| FIND-017 | Suggestion | `messages.message_ts` ↔ envelope `timestamp` name mismatch | |
| FIND-018 | Suggestion | §11.2 missing wrapper-side env vars | `AMC_BASE_URL`, `AMC_AGENT_ID` not in the env-var table |
| FIND-019 | Suggestion | §3.2 "Schema-divergence-from-blueprint tracked" is process, not outcome | |
| FIND-020 | Suggestion | `direction='outbound'` vs `allowlist_status='outbound'` enum-value overlap | |

### Recommended Next Steps

- All 14 warning-severity findings are now resolved. The spec is in implementation-ready shape with respect to this analysis.
- The 6 remaining suggestions (FIND-015 through FIND-020) are polish — reasonable to defer or batch into a single follow-up pass once Phase 1 is underway. None are blocking.

---

## Findings

### Critical

No critical findings.

---

### Warnings

#### FIND-001: iMessage IDs are not consistently namespaced

- **Category**: Inconsistencies
- **Location**: §7.3.1 envelope (lines 622–645, 651, 653); §5.7 allowlist example (lines 396–414); §7.3.3 `messages` field defs (lines 759, 761)
- **Current Text**: §7.3.1 line 651 — `` `channel_id` (string): platform-namespaced. iMessage uses E.164 phone or Apple ID email; Discord uses `discord:<dm|channel>:<id>`. ``; envelope example line 624 — `"channel_id": "+15551234567"`; allowlist example lines 397–401 — `id = "+15551234567"` for iMessage, `id = "discord:user:99887766554433"` for Discord.
- **Issue**: The spec states `sender.id` and `channel_id` are "platform-namespaced" (§7.3.1 lines 651, 653) but immediately defines iMessage IDs as raw E.164 phone or email — i.e., not namespaced — while Discord uses an explicit `discord:...` prefix. The allowlist example mirrors the inconsistency: iMessage uses raw `"+15551234567"`, Discord uses `"discord:user:..."`. The "platform-namespaced" claim is therefore false for iMessage. With `senders.sender_id` and `channels.channel_id` declared as single-column PKs in §7.3.2 ERD, raw E.164 phone numbers and `discord:user:...` strings share the same key space — a (theoretical) collision risk.
- **Impact**: PK collision risk; the "platform-namespaced" mental model the spec sets up is internally violated; a future third connector author has no rule to follow ("namespace like Discord, or raw like iMessage?").
- **Recommendation**: Either (a) make iMessage namespacing explicit too — `imessage:phone:+15551234567`, `imessage:email:foo@bar.com` — and update §7.3.1 examples, allowlist example, and §7.3.3 field descriptions; OR (b) keep iMessage IDs raw, drop the "platform-namespaced" wording, and change the PKs in §7.3.2 to composite `(source, channel_id)` and `(source, sender_id)`.
- **Status**: Resolved (2026-05-02 — applied option (b): kept iMessage IDs raw, dropped "platform-namespaced" wording in §7.3.1, composite PKs added in §7.3.2 per FIND-010)

---

#### FIND-002: `senders.allowlist_status` enum undefined; conflicts with `messages.allowlist_status`

- **Category**: Inconsistencies
- **Location**: §7.3.2 ERD (lines 698–706); §7.3.3 `messages` field defs (line 765)
- **Current Text**: §7.3.3 line 765 — `` `allowlist_status` | TEXT | NOT NULL CHECK IN ('allowed','unknown','outbound') | `outbound` for messages we sent ``; §7.3.2 lines 698–706 — `SENDERS { ... string allowlist_status ... }` (no enum stated).
- **Issue**: Both `messages` and `senders` have a column named `allowlist_status`. The `messages` enum is `('allowed','unknown','outbound')`; `senders.allowlist_status` has no enum defined. The value `'outbound'` makes no semantic sense on a sender row (a sender isn't "outbound"; a message is). Either the columns serve different purposes (and shouldn't share a name) or they're meant to be derived from each other (and one needs to be the source of truth).
- **Impact**: Implementer guesses at the sender enum; risk of derived state diverging (e.g., `senders.allowlist_status='unknown'` while `messages.allowlist_status='allowed'`); migrations may apply different CHECK constraints than intended.
- **Recommendation**: In §7.3.3, add a `senders` field-definition table including `allowlist_status TEXT NOT NULL CHECK IN ('allowed','unknown')`. Add a sentence stating `senders.allowlist_status` is the source of truth, and `messages.allowlist_status` is a denormalized snapshot at INSERT time (with `'outbound'` reserved for agent-sent messages where allowlisting doesn't apply).
- **Status**: Resolved (2026-05-02 — added `senders` field-def table to §7.3.3 with explicit enum `('allowed','unknown')`; clarified `messages.allowlist_status` as denormalized snapshot of the senders source-of-truth)

---

#### FIND-003: iMessage send retry: "3 retries" vs "3 attempts" — off by one

- **Category**: Inconsistencies
- **Location**: §5.2 acceptance criteria (line 244); §5.2 edge cases (line 258); §6.4 (line 516)
- **Current Text**:
  - §5.2 line 244 — `iMessage send: 3 retries with backoff before marking ` `send_failed.`
  - §5.2 line 258 — `AppleScript times out | osascript hangs > 10 s | Killed, retried up to 3 times, then ` `send_failed.`
  - §6.4 line 516 — `Send retry policy: 3 attempts on iMessage AppleScript failure with backoff; library defaults on Discord 5xx.`
- **Issue**: "3 retries" means original attempt + 3 retries = **4 total attempts**. "3 attempts" means **3 total attempts**. The two normative sections disagree by one.
- **Impact**: Implementer codes against one phrasing; tests written from the other phrasing fail; observed `send_failed` rate disagrees with `attempt` counts in logs.
- **Recommendation**: Pick one phrasing — recommend "**up to 4 total attempts (1 initial + 3 retries)**" — and use it identically in §5.2 acceptance criteria, §5.2 edge-cases table, and §6.4. Apply the same disambiguation to the §5.5 webhook retry policy ("5 attempts" already matches itself; just verify).
- **Status**: Resolved (2026-05-02 — standardized to "up to 4 total attempts (1 initial + 3 retries)" in §5.2 acceptance criteria, §5.2 edge-cases table, and §6.4)

---

#### FIND-004: §7.4.2 uses `404 NOT_FOUND` but stable code is `MESSAGE_NOT_FOUND`

- **Category**: Inconsistencies
- **Location**: §7.4.2 (line 851); §7.4.12 stable codes list (line 1002)
- **Current Text**:
  - §7.4.2 line 851 — `**Response**: ` `200 OK` ` with envelope or ` `404 NOT_FOUND.`
  - §7.4.12 line 1002 — `Stable codes: ` `UNAUTHORIZED, AGENT_ID_REQUIRED, VALIDATION_ERROR, IDEMPOTENCY_KEY_REUSE, RATE_LIMITED, MESSAGE_NOT_FOUND, CHANNEL_NOT_FOUND, ATTACHMENT_TOO_LARGE_FOR_PLATFORM, PLATFORM_AUTH, PLATFORM_SEND_FAILED, INTERNAL_ERROR.`
- **Issue**: §7.4.2's `NOT_FOUND` is not in the stable-codes list. §7.4.3 line 866 uses the correct `MESSAGE_NOT_FOUND`. The §7.4.2 form will produce a divergent error contract.
- **Impact**: Client error-handling code keyed on `MESSAGE_NOT_FOUND` won't match §7.4.2's actual response; OpenAPI-generated clients will see two different 404 codes for the same resource family.
- **Recommendation**: Change §7.4.2 line 851 to `**Response**: 200 OK with envelope, or 404 with code=MESSAGE_NOT_FOUND.` Add a brief inline JSON example matching §7.4.12's standard error envelope.
- **Status**: Resolved (2026-05-02 — §7.4.2 now uses `404 with code=MESSAGE_NOT_FOUND` and cross-references §7.4.12; also added explicit Authentication line)

---

#### FIND-005: `POST /messages/send` missing `404 CHANNEL_NOT_FOUND` response

- **Category**: Missing Information
- **Location**: §7.4.5 response section (lines 913–922); §5.2 error handling table (line 266)
- **Current Text**:
  - §5.2 line 266 — `Channel not found | ` `404` ` with ` `code=CHANNEL_NOT_FOUND` ` | No retry`
  - §7.4.5 lines 920–922 — `` `429` — `code=RATE_LIMITED` with `Retry-After`. `422` — `code=IDEMPOTENCY_KEY_REUSE` if key has been used with a different body. `502` — `code=PLATFORM_SEND_FAILED` after retries exhausted. `` (no 404 listed)
- **Issue**: §5.2's error handling table promises a `404 CHANNEL_NOT_FOUND` on send-to-unknown-channel, but the API spec for `POST /messages/send` doesn't document that response. The endpoint contract is incomplete.
- **Impact**: FastAPI's auto-generated OpenAPI omits the 404; clients written from `/openapi.json` won't expect 404 from send; integration tests against §7.4.5 won't assert the §5.2 promise.
- **Recommendation**: Add to §7.4.5 response section: `` `404` — `code=CHANNEL_NOT_FOUND` if `channel_id` is not registered. ``
- **Status**: Resolved (2026-05-02 — added `404 — code=CHANNEL_NOT_FOUND` line to §7.4.5 response section with cross-reference to §5.2; also reordered the existing 4xx/5xx list into ascending HTTP-status order)

---

#### FIND-006: Per-endpoint `X-Agent-ID` requirement is documented inconsistently

- **Category**: Ambiguities
- **Location**: §7.4 preamble (line 813); §7.4.1 (line 819); §7.4.2 (lines 847–851); §7.4.3 (lines 858–861); §7.4.4 (lines 873–877); §7.4.5 (lines 894–898); §7.4.6 (line 928)
- **Current Text**:
  - §7.4 preamble line 813 — `Reading endpoints additionally require ` `X-Agent-ID: <name>` ` (where noted).`
  - §7.4.1 line 819 — `**Authentication**: Bearer + ` `X-Agent-ID` ` required.` (explicit, correct)
  - §7.4.2 — no Authentication line; no `X-Agent-ID` example.
  - §7.4.3 — `X-Agent-ID` shown in example header but no Authentication declaration.
  - §7.4.4 — `X-Agent-ID` shown in example but no Authentication declaration.
  - §7.4.5 — no `X-Agent-ID` shown; no Authentication declaration.
  - §7.4.6 — silent on auth entirely.
- **Issue**: Only §7.4.1 has an explicit "Authentication" line. The other five endpoints leave the `X-Agent-ID` requirement to inference from examples or from the §7.4 preamble's "where noted" hedge. REQ-AMC-003 line 280 implies `X-Agent-ID` is required on `/messages/unread` and `/messages/mark_read`, but says nothing about `/messages/{id}`, `/messages/context`, `/messages/send`, or `/typing`.
- **Impact**: Implementer codes inconsistent middleware: some routes 400 on missing header, others accept; clients see surprising auth failures; tests don't cover the disagreement.
- **Recommendation**: Add an `**Authentication**: Bearer` or `**Authentication**: Bearer + X-Agent-ID required` line to every §7.4.x subsection. Add a small summary table at the top of §7.4 listing each endpoint × required headers. Decide explicitly whether `/messages/{id}` and `/messages/context` need `X-Agent-ID` (recommend: yes, since they're read paths that filter by allowlist visibility).
- **Status**: Resolved (2026-05-02 — added explicit Authentication line to §7.4.2 through §7.4.10 and a header-requirements summary table at the top of §7.4; `/messages/{id}` and `/messages/context` confirmed as Bearer + X-Agent-ID)

---

#### FIND-007: Idempotency-Key on `mark_read` — required, optional, or vestigial?

- **Category**: Ambiguities
- **Location**: §7.4.4 example (lines 873–881); §5.3 acceptance criteria (line 282)
- **Current Text**:
  - §7.4.4 lines 873–881 — example shows `Idempotency-Key: 0d2f...` header.
  - §5.3 line 282 — `mark_read UPSERTs ` `(message_id, agent_id, read_at)` ` rows; idempotent.`
- **Issue**: `mark_read` is inherently idempotent via UPSERT semantics. The example shows an `Idempotency-Key` header but neither §5.3 nor §7.4.4 explains what it adds beyond the UPSERT, whether it's required, or what the response semantics are on key reuse.
- **Impact**: Ambiguous middleware behavior — does `mark_read` consume `idempotency_keys` rows for no semantic gain? Implementer may build either flavor; tests may diverge.
- **Recommendation**: Either (a) drop the `Idempotency-Key` header from the §7.4.4 example since UPSERT covers it, OR (b) add a sentence in §5.3 explaining what it adds (e.g., "lets duplicate calls return identical `marked_count` even if the underlying state changed between the two calls"). Mark the header as `optional` either way.
- **Status**: Resolved (2026-05-02 — applied option (a): dropped `Idempotency-Key` from §7.4.4 example; added explicit "not required (UPSERT semantics make this endpoint naturally idempotent)" note in the Authentication line)

---

#### FIND-008: Discord "REST replay if supported" is hand-wavy in P0 acceptance criteria

- **Category**: Ambiguities
- **Location**: §5.1 Edge Cases table (line 214)
- **Current Text**: `Discord gateway disconnects mid-conversation | Gateway WS close | Library-level reconnect with backoff; resume from last sequence ID; missed messages are pulled via REST replay if supported, else accepted as a soft gap`
- **Issue**: "REST replay if supported" is unclear. Discord's gateway offers `RESUME` for in-buffer-window gaps, but for messages that fall outside the resume buffer, there's no generic "replay missed messages since timestamp X" REST endpoint — recovery requires per-channel `GET /channels/{id}/messages?after=<last_seen_id>`. "If supported" is doing a lot of work in a P0 acceptance criterion.
- **Impact**: Implementer either overbuilds a per-channel polling fallback they don't understand, or skips replay and silently loses messages on long disconnects, both diverging from spec intent.
- **Recommendation**: Tighten to one of: (a) "Rely on `RESUME` session; accept any post-buffer-window gap as lost in v1," OR (b) "After disconnect > N minutes, REST-poll each known channel via `messages?after=<last_seen_id>`." Pick one and remove "if supported".
- **Status**: Resolved (2026-05-02 — applied option (a): RESUME-only with soft gap; v1 accepts post-buffer-window messages as lost; REST-poll replay is post-v1)

---

#### FIND-009: SIGHUP allowlist reload — "version captured at message time" is unspecified mechanism

- **Category**: Ambiguities
- **Location**: §5.7 acceptance criteria (line 388–391)
- **Current Text**: `SIGHUP reloads the file; in-flight messages use the version captured at message time.`
- **Issue**: "Captured at message time" implies a snapshot mechanism but the spec doesn't say what snapshots — per-message copy of the allowlist? A version counter? Or just "we resolve at message INSERT and never re-resolve"? OQ-4 already addresses one downstream consequence (whether quarantined messages migrate after a reload), but the underlying mechanism is left undefined.
- **Impact**: Implementer guesses (most likely: resolve at INSERT, persist on the row, never re-resolve), which is fine but means OQ-4's default ("no migration") is implicitly assumed. If a future operator expects atomic snapshots across an in-flight batch, they'll be surprised.
- **Recommendation**: Add one sentence to §5.7: "Allowlist resolution happens once per message at INSERT time; the resolved `sender_id`, `display_name`, `person_id`, and `allowlist_status` are persisted on the message and sender rows and are not recomputed against later allowlist versions."
- **Status**: Resolved (2026-05-02 — appended the recommended sentence to the §5.7 SIGHUP acceptance criterion; also added an explicit cross-reference to OQ-4 for the related "do quarantined messages migrate?" question)

---

#### FIND-010: PK uniqueness for `channels.channel_id` and `senders.sender_id` not scoped by `source`

- **Category**: Inconsistencies
- **Location**: §7.3.2 ERD (lines 691–706)
- **Current Text**:
  - `CHANNELS { string channel_id PK; string source; ... }`
  - `SENDERS { string sender_id PK; string source; ... }`
- **Issue**: Single-column PKs on `channel_id` and `sender_id`. Combined with FIND-001 (iMessage IDs raw, Discord IDs prefixed), there's a theoretical collision path between platforms. Even after FIND-001 is resolved, declaring composite PKs makes the cross-source uniqueness invariant explicit and survives a future "connector forgot to prefix" bug.
- **Impact**: Same row representing two distinct entities silently — corrupts message routing and `identity_links` derivation.
- **Recommendation**: Change PKs in §7.3.2 to composite: `(source, channel_id)` on `channels`, `(source, sender_id)` on `senders`. Update FK declarations on `messages` to match. (This is moot if FIND-001 is resolved by namespacing iMessage too — but composite PK is still more defensive.)
- **Status**: Resolved (2026-05-02 — composite PKs `(source, channel_id)` on `channels` and `(source, sender_id)` on `senders` applied in §7.3.2; `messages` FK declarations updated in §7.3.3 to reference the composite keys)

---

#### FIND-011: `channels`, `senders`, `identity_links`, `attachments`, `connector_state` lack field definitions in §7.3.3

- **Category**: Missing Information
- **Location**: §7.3.3 (line 751)
- **Current Text**: §7.3.3 heading — `Field definitions (selected tables)` — only documents `messages`, `message_reads`, `webhook_deliveries`, `idempotency_keys`.
- **Issue**: Five ERD entities have no field-level definition table: `channels` (what populates `last_seen_message_id`?), `senders` (what enum values for `allowlist_status` — see FIND-002 — and what populates `first_seen`/`last_seen`?), `identity_links` (composition rules from `person_id` groupings?), `attachments` (`bytes_path` format relative or absolute? `original_url_or_path` — which?), `connector_state` (is `cursor` a stringified ROWID for iMessage and a Discord session-id-plus-seq for Discord?).
- **Impact**: Migration author guesses at types/constraints; reviewer can't verify schema consistency; a third connector author has no schema contract for `connector_state.cursor`.
- **Recommendation**: Add field-definition tables for the five missing entities. Or, if intentionally deferred, change the §7.3.3 heading to "Field definitions (most important tables)" and add a one-line note: "Remaining tables (`channels`, `senders`, `identity_links`, `attachments`, `connector_state`) are documented inline in their respective REQ-AMC-NNN features."
- **Status**: Resolved (2026-05-02 — applied option (a): added field-definition tables for all five missing entities — `channels`, `senders`, `identity_links`, `attachments`, `connector_state` — to §7.3.3 with full type/constraint/description columns and indexes)

---

#### FIND-012: ULID, HMAC, chat.guid, RFC 3339 used but not in glossary

- **Category**: Missing Information
- **Location**: §15.1 Glossary (lines 1419–1439); used throughout — e.g., §7.3.1 line 622 (ULID), line 659 (RFC 3339); §6.2 line 486 (HMAC); §7.5 line 1033 (`chat.guid`).
- **Current Text**: §15.1 Glossary defines 16 terms: Adapter, Allowlist, Connector, Envelope, FDA, Idempotency-Key, MCP, MCP wrapper, Per-agent cursor, `person_id`, Quarantine, ROWID, Soak, Tapback, Token bucket, WAL, Webhook. Missing: ULID, HMAC, RFC 3339, `chat.guid`.
- **Issue**: ULID is used as the `id` format for `messages`, `attachments`, `webhook_deliveries` (§7.3.1, §7.3.3) without a definition or link. HMAC is used in §6.2, §5.5, §7.4.11 without explanation. `chat.guid` appears in workflow diagrams (§4.3, §7.5) without definition. The §3.2 metric "A teammate completes setup from `SETUP.md` in ≤ 60 minutes" implies the spec should be self-contained for someone unfamiliar with the codebase.
- **Impact**: A teammate reaching the spec via SETUP.md hits acronyms they may not know; affects spec self-containment.
- **Recommendation**: Add to §15.1: `ULID` (Universally Unique Lexicographically Sortable Identifier — 26-char base32; sorts by creation time); `HMAC` (Hash-based Message Authentication Code — used here with SHA-256); `chat.guid` (the `chat.guid` column from `chat.db`'s `chat` table; a stable identifier for an iMessage conversation); and either define RFC 3339 inline at first use or add a glossary entry pointing to the IETF spec.
- **Status**: Resolved (2026-05-02 — added 4 entries to §15.1 glossary: ULID, HMAC, `chat.guid`, RFC 3339; alphabetized in place)

---

#### FIND-013: §6.2 Authorization role table implies enforcement that the same section explicitly disclaims

- **Category**: Ambiguities
- **Location**: §6.2 Authorization (lines 488–495)
- **Current Text**:
  - Lines 490–493 — table with two roles: `Agent (any X-Agent-ID)` permitted "Read inbound, send outbound, mark-read scoped to its own `agent_id`, read context"; `Operator (no X-Agent-ID)` permitted "All of the above + `/healthz`, `/messages/quarantine`, `/openapi.json`, `/docs`, `/attachments/{id}`".
  - Line 495 — `There is no role separation enforced by token in v1: a single bearer token grants both. ` `X-Agent-ID` ` is for cursor-isolation, not auth.`
- **Issue**: The table presents an access-control matrix; the disclaimer below says it isn't actually enforced. A reader reasonably asks: "If I send `X-Agent-ID: claude-code` with a request to `/healthz`, do I get 200 or 403?" The answer per the disclaimer is 200, but the table reads like 403.
- **Impact**: Implementer adds enforcement (which the disclaimer says NOT to do) or skips it (then the table is misleading); auditors using the spec to verify access control draw wrong conclusions.
- **Recommendation**: Either (a) rename the table heading to "Conventional Use (not enforced in v1)" and keep the disclaimer; OR (b) actually enforce: route `/healthz`, `/messages/quarantine`, etc. only when `X-Agent-ID` is absent, and update the disclaimer to match. Recommend (a) for v1 scope.
- **Status**: Resolved (2026-05-02 — applied option (a): renamed §6.2 heading to "Authorization (Conventional Use — not enforced in v1)"; added clarifying lead-in sentence; kept the role table and existing disclaimer)

---

#### FIND-014: §9.0 heading "Pre-Phase 0" is misleading

- **Category**: Structure Issues
- **Location**: §9.0 (line 1142)
- **Current Text**: `### 9.0 Pre-Phase 0 — Spike POC (~half day)`
- **Issue**: The section is titled "Pre-Phase 0" but its content (deliverables, completion criteria, checkpoint gate) is structurally identical to §9.1–§9.4 phase blocks. There is no separate Phase 0 elsewhere — this IS Phase 0. The "Pre-" prefix creates a false expectation that another section follows before §9.1 Phase 1.
- **Impact**: Skim-reading confusion; tracker/issue-creation tools may create a phantom "Phase 0" milestone in addition to "Pre-Phase 0".
- **Recommendation**: Rename to `### 9.0 Phase 0 — Spike POC (~half day)`. Verify no other section cross-references "Pre-Phase 0" by name.
- **Status**: Resolved (2026-05-02 — renamed §9.0 heading to "Phase 0 — Spike POC (~half day)"; verified via grep that no other section references "Pre-Phase 0")

---

### Suggestions

#### FIND-015: §3.2 "Setup repeatability" measurement uses n=1 sample

- **Category**: Inconsistencies (metric vs. statistical claim)
- **Location**: §3.2 (line 64)
- **Current Text**: `Setup repeatability | N/A | A teammate completes setup from ` `SETUP.md` ` in ≤ 60 minutes | One witnessed install attempt | Phase 4`
- **Issue**: A single observation isn't a metric — it's an acceptance criterion. The other rows in §3.2 are aggregate metrics (P95 latencies, ≥ 95% delivery rate). This row is a one-shot test.
- **Impact**: Minor — internal consistency with the rest of the table.
- **Recommendation**: Either reframe as an acceptance criterion in §9.4's checkpoint gate (where it already lives) and remove from §3.2; OR keep it in §3.2 with explicit framing: "Acceptance criterion (n=1; this is a witnessed install, not a statistic)."
- **Status**: Pending

---

#### FIND-016: §7.4.2 lacks response example

- **Category**: Structure Issues (formatting consistency)
- **Location**: §7.4.2 (line 851)
- **Current Text**: `**Response**: ` `200 OK` ` with envelope or ` `404 NOT_FOUND.`
- **Issue**: Every other §7.4.x endpoint shows an explicit JSON request and/or response example (§7.4.1 has both, §7.4.3 has both, §7.4.4 has both, §7.4.5 has both). §7.4.2 has only this one-liner.
- **Impact**: Minor — inconsistent presentation; reader has to follow the §7.3.1 envelope cross-reference manually.
- **Recommendation**: Add a `**Request**` example and a one-line note `**Response**: 200 OK with the full normalized envelope (see §7.3.1).` followed by `**Errors**: 404 with code=MESSAGE_NOT_FOUND.` (this also resolves FIND-004).
- **Status**: Pending

---

#### FIND-017: `messages.message_ts` (DB) vs envelope `timestamp` field-name mismatch

- **Category**: Inconsistencies (naming)
- **Location**: §7.3.1 line 641; §7.3.3 line 766
- **Current Text**:
  - §7.3.1 line 641 — `"timestamp": "2026-04-25T15:32:11Z"`
  - §7.3.3 line 766 — `` `message_ts` | TEXT (ISO 8601) | NOT NULL | Platform-claimed timestamp ``
- **Issue**: The envelope field is `timestamp`; the DB column is `message_ts`. The mapping is implicit. Also, the DB column `created_at` (adapter ingest time) has no envelope counterpart, but the spec doesn't say so explicitly.
- **Impact**: Minor — a reader has to deduce the mapping; ORM models will need explicit aliasing.
- **Recommendation**: Add a note under §7.3.3 messages table: "`message_ts` maps to `envelope.timestamp`. `created_at` is adapter-internal and not exposed in the envelope."
- **Status**: Pending

---

#### FIND-018: §11.2 missing wrapper-side env vars (`AMC_BASE_URL`, `AMC_AGENT_ID`)

- **Category**: Missing Information
- **Location**: §11.2 env-var table (lines 1335–1347); §5.6 acceptance criterion (line 370)
- **Current Text**:
  - §5.6 line 370 — `Wrapper config: ` `AMC_BASE_URL` ` (default ` `http://127.0.0.1:8080` `), ` `AMC_BEARER_TOKEN` `, ` `AMC_AGENT_ID` `.`
  - §11.2 lists adapter-only env vars (no `AMC_BASE_URL`, no `AMC_AGENT_ID`).
- **Issue**: A reader doing end-to-end deployment from §11 alone won't see the wrapper-side env vars. `AMC_BEARER_TOKEN` is shared between adapter and wrapper but only documented as adapter-side.
- **Impact**: Minor — leads to config-step gaps in SETUP.md if §11 is the source for deployment configuration.
- **Recommendation**: Add a §11.2.1 "Wrapper environment variables" subsection with `AMC_BASE_URL`, `AMC_BEARER_TOKEN`, `AMC_AGENT_ID`; or add a "wrapper" column to the existing §11.2 table marking which vars are read by which process.
- **Status**: Pending

---

#### FIND-019: §3.2 "Schema-divergence-from-blueprint tracked" is process, not outcome

- **Category**: Ambiguities
- **Location**: §3.2 (line 65)
- **Current Text**: `Schema-divergence-from-blueprint tracked | Partial | All v1 divergences updated in blueprint | Diff blueprint vs. spec; resolve | End of Phase 1`
- **Issue**: This is a binary done/not-done deliverable, not a measurable success metric. The same item is tracked appropriately in §15.3 ("Required Blueprint Updates (post-Phase 1)") and as a §9.1 checkpoint-gate item.
- **Impact**: Minor — clutters the success-metrics table with a non-metric.
- **Recommendation**: Remove from §3.2; it already lives in §15.3 and §9.1's checkpoint gate.
- **Status**: Pending

---

#### FIND-020: `direction='outbound'` and `allowlist_status='outbound'` enum-value overlap

- **Category**: Inconsistencies
- **Location**: §7.3.3 messages table (lines 764–765); §14 OQ-3 (line 1411)
- **Current Text**:
  - §7.3.3 line 764 — `` `direction` | TEXT | NOT NULL CHECK IN ('inbound','outbound') | Direction through AMC ``
  - §7.3.3 line 765 — `` `allowlist_status` | TEXT | NOT NULL CHECK IN ('allowed','unknown','outbound') | `outbound` for messages we sent ``
- **Issue**: The string `'outbound'` is a valid value in two different enums on the same row, meaning two different things. `allowlist_status='outbound'` is really "N/A — agent-sent message; allowlisting doesn't apply." It collides with the `direction` enum's `'outbound'` purely lexically and creates a "smell" — the implication is that the schema is doing double duty in one column.
- **Impact**: Minor naming smell; will confuse SQL queries that filter on either column.
- **Recommendation**: Rename the third value of `allowlist_status` to `'not_applicable'` (or drop it: let `direction='outbound' → allowlist_status IS NULL` carry the semantics). This dovetails with OQ-3, which is already considering a separate `delivery_status` column — recommend resolving them together.
- **Status**: Pending

---

## Resolution Summary

*(Updated after interactive review.)*

**Review Session**: Pending

| Metric | Count |
|--------|-------|
| Total Findings | 20 |
| Resolved | 0 |
| Skipped | 0 |
| Remaining | 20 |

### Resolved Findings

*(None yet.)*

### Skipped Findings

*(None yet.)*

---

## Analysis Methodology

This analysis was performed using Full-Tech criteria from `references/analysis-criteria.md`.

- **Sections Checked**: §1 Executive Summary, §2 Problem Statement, §3 Goals & Success Metrics, §4 User Research, §5 Functional Requirements (REQ-AMC-001 through REQ-AMC-009), §6 Non-Functional Requirements, §7 Technical Architecture (system overview, tech stack, data models, API specs, integration points, technical constraints), §8 Scope, §9 Implementation Plan (Phases 0–4), §10 Testing Strategy, §11 Deployment & Operations, §12 Dependencies, §13 Risks, §14 Open Questions, §15 Appendix.
- **Criteria Applied**: Full-Tech checklist — system architecture present, API specifications complete, data models defined, performance SLAs quantified, testing strategy outlined, deployment plan provided. Also applied cross-depth checks for internal consistency (feature naming, priority alignment, metric-to-goal mapping), completeness (no TBDs in critical sections except where explicitly tracked in §14 OQs), measurability, and clarity.
- **Out of Scope**: §14 OQ-1 through OQ-7 were not flagged as findings (intentional, owned, dated deferrals). §7.3.1, §7.4, and §15.3 self-documented divergences from the blueprint were not flagged as findings (tracked Phase 1 work). §6.5 "Accessibility — Not applicable" was not flagged (justified non-applicability for a no-UI service). §7.6 "Not applicable — no existing codebase" was not flagged (correctly empty with rationale).
