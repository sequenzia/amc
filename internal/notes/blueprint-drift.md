# Blueprint Drift (Phase 4 reconciliation pass)

**Status**: Resolved within Phase 4 reconciliation (see "Resolution" column).
**Date**: 2026-05-03
**Discovered during**: Task #80 (final blueprint reconciliation, end of Phase 4).
**Predecessor**: Task #45 produced `internal/notes/spec-code-divergences.md` (still open: D-1, D-2 — those are spec-vs-code, not blueprint-vs-spec).
**Reference**: `specs/agent-messaging-channel-SPEC.md` v1.1, dated 2026-05-03.

---

## Purpose

Phase 1 reconciliation (Task #45) updated the blueprint's §3 envelope and §5.3
storage to match the spec, and added the post-v1 tool-surface and stdio-only
notes to §6/§7. Between Phase 1 and Phase 4, the spec gained additional surface
area that the blueprint never picked up. This pass walks every numbered section
and either reconciles or documents the gap.

The blueprint contains **no mermaid diagrams** in v1 — only an ASCII
architecture sketch in §2 and JSON schema blocks in §3 / §6.1. The mermaid
diagrams (§4.2 user-journey flowchart, §4.3 inbound-Discord and inbound-iMessage
sequence diagrams, §7.1 system-overview flowchart, §7.3.2 ER diagram, §7.5
iMessage-path and Discord-path sequence diagrams) all live in the spec; they
were verified against the as-built data flow during this pass and are consistent
with what's in `amc/`.

---

## Drift items found and resolved

### B-1: §2 ASCII architecture diagram — accuracy spot check

| Surface | Detail |
|---------|--------|
| Blueprint §2 | ASCII diagram showing Agent → MCP wrapper / direct HTTP → Adapter → connectors → SQLite |
| As built | `amc/app.py` (FastAPI) with `amc/api/*` routes; `amc/connectors/{discord,imessage}` as background tasks; `amc/core/db.py` SQLite via aiosqlite; `amc/core/attachments.py` re-host store on disk; `amc/core/webhook.py` outbound delivery worker |

**Resolution**: Diagram is accurate at the layer/box level. Two minor omissions
(filesystem attachment store, outbound webhook receiver) are present in the
spec's §7.1 mermaid diagram and are not worth duplicating in the ASCII version.
Blueprint §2 prose already says "Webhook outbound for new messages" inside the
adapter box. **No edit required.**

---

### B-2: §5.1 endpoint table — incomplete vs spec §7.4

| Surface | Endpoints listed |
|---------|------------------|
| Blueprint §5.1 | `GET /messages/unread`, `GET /messages/{id}`, `GET /messages/context`, `POST /messages/mark_read`, `POST /messages/send`, `POST /typing`, plus webhook |
| Spec §7.4 | All of the above **plus** `GET /messages/quarantine` (§7.4.8), `GET /attachments/{id}` (§7.4.9), `GET /healthz` (§7.4.10), `GET /openapi.json` / `GET /docs` |
| As built | All of the spec's surface present in `amc/api/` (`messages_quarantine.py`, `attachments_get.py`, `healthz.py`) plus FastAPI's auto-generated `/openapi.json` and `/docs` |

**Resolution**: Blueprint §5.1 was authored before the operator-facing endpoints
were settled. Updated this pass to add a "Operator / system endpoints" subgroup
listing the four extra endpoints, with a pointer to spec §7.4 for full request /
response shapes. Blueprint stays high-level; the spec is the request/response
source of truth.

---

### B-3: §8 Implementation Phases — missing Phase 0

| Surface | Phases |
|---------|--------|
| Blueprint §8 | Phase 1 (Adapter + Discord), Phase 2 (iMessage), Phase 3 (MCP wrapper), Phase 4 (Hardening) |
| Spec §9 | **Phase 0** (test fixtures + platform stubs) **plus** Phases 1–4 |
| As built | Phase 0 deliverables shipped (`tests/fixtures/chat.db`, `tests/fakes/{applescript,discord_gateway,discord_rest}.py`, `internal/notes/phase0-findings.md`); spec v1.1 reframed all later phases to depend on these fakes |

**Resolution**: Blueprint §8 was written before spec v1.1 added the autonomous-
build-acceptance reframing. Updated this pass to insert a "Phase 0" entry at
the top of §8 with a one-line description, and to add a footnote that the
detailed completion criteria for every phase live in spec §9. The blueprint
phase list stays short by design.

---

### B-4: §9 Open Questions — shallower than spec §14

| Surface | OQs |
|---------|-----|
| Blueprint §9 | Three resolved (group chat, attachment strategy, multi-agent contention), two still open (read receipts/tapbacks, permission gating) |
| Spec §14 | Seven OQs (OQ-1 through OQ-7); OQ-1 resolved (see `internal/notes/oq-1-decision.md`), OQ-2/4/5/6/7 resolved during Phase 1 implementation, OQ-3 settled during schema review |

**Resolution**: The blueprint and the spec address open questions at different
altitudes — blueprint §9 lists "future product" questions (will we ever build
group chat? will tapbacks be a thing?) while spec §14 lists "implementation
choice" questions (what does `marked_count` count? how do collisions resolve?).
They don't conflict. Updated blueprint §9 to add a "See spec §14 for v1
implementation OQs" pointer. The two surfaces stay deliberately separate.

---

### B-5: footer reconciliation note — Phase-1-only scope

| Surface | Footer text |
|---------|-------------|
| Blueprint (pre-Phase-4) | `*Reconciled with specs/agent-messaging-channel-SPEC.md v1.1 at end of Phase 1.*` |
| Required by Task #80 | `Reconciled at v1 acceptance, against specs/agent-messaging-channel-SPEC.md v1.1 and the Phase 4 stability run.` |

**Resolution**: Updated this pass.

---

### B-6: README pointer — none exists

| Surface | Detail |
|---------|--------|
| `internal/blueprints/README.md` | **Does not exist**; `internal/blueprints/` contains only `agent-messaging-channel.md` |
| Blueprint intro | Single-line subtitle; no spec pointer |

**Resolution**: Rather than create a `README.md` that would then duplicate the
blueprint's own intro, this pass adds a "Source of truth (v1)" callout block
immediately after the blueprint's title that points readers at
`specs/agent-messaging-channel-SPEC.md` v1.1 for v1 contracts (envelope shape,
REST/MCP surface, schema field types, error codes, env vars). The blueprint
remains the architectural source of truth; the spec is the v1 implementation
contract.

---

## Items NOT addressed in this pass

### Spec ↔ code divergences from Phase 1 (D-1, D-2)

These remain open in `internal/notes/spec-code-divergences.md`:

- **D-1**: `VALIDATION_FAILED` (in code, `amc/core/errors.py`) vs `VALIDATION_ERROR` (spec §7.4.12 stable-codes list).
- **D-2**: Default DB filename `state.db` (in code) vs `amc.db` (spec §11.2 `AMC_DB_PATH` row).

Both require a user decision before either the spec or the code is edited.
Task #80 cannot resolve them — the spec is locked under Task #80's allowed-paths
list, and the code has settled tests around the current strings. They are
**referenced** from the blueprint's reconciliation footer (preserved from the
Phase 1 reconciliation footnote) so future readers know the canonical strings
diverge from the spec by name.

---

## How to close the predecessor file

When D-1 / D-2 are resolved (user picks "code wins" or "spec wins" per item):

1. Apply the chosen edit (spec or code) in a single commit referencing
   `spec-code-divergences.md`.
2. Move both `spec-code-divergences.md` and this `blueprint-drift.md` to
   `internal/notes/archive/` (or delete) and remove the reconciliation footnote
   block at the end of `internal/blueprints/agent-messaging-channel.md`.
