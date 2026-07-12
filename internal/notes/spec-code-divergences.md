# Spec vs Code Divergences (Phase 1 Reconciliation)

**Status**: Open — needs user decision
**Date**: 2026-05-03
**Discovered during**: Task #45 (blueprint reconciliation, end of Phase 1)
**Related**: spec §7.4.12, spec §11.2

---

## Purpose

Tracks wording / value mismatches between `specs/agent-messaging-gateway-SPEC.md` v1.1
and the in-tree implementation found at end of Phase 1. Each item below needs the
user to pick the canonical form. Until that happens, the blueprint reconciliation
note (`internal/blueprints/agent-messaging-gateway.md`, end of file) records the
drift but does **not** make the call.

The recommendation in every case below is "code wins, update the spec later" —
the code is what tests run against, and changing it now risks breaking 168 tests
of in-flight work for cosmetic reasons.

---

## D-1: Validation error code — `VALIDATION_FAILED` vs `VALIDATION_ERROR`

| Surface | Value | Locations |
|---------|-------|-----------|
| Code (canonical in tree) | `VALIDATION_FAILED` | `amg/core/errors.py:70`, used by `amg/api/messages_unread.py`, `amg/api/messages_quarantine.py`, `amg/api/messages_context.py` |
| Spec §7.4.12 | `VALIDATION_ERROR` | spec line 1098 (the "Stable codes" list) |

Both names are clear; neither is more correct. The full set of stable codes in
the spec uses `_FAILED` only for `PLATFORM_SEND_FAILED`, with everything else
using bare nouns (`UNAUTHORIZED`, `RATE_LIMITED`) or `_ERROR` (`INTERNAL_ERROR`).
Strictly by that pattern, `VALIDATION_ERROR` is the more spec-consistent name.

**Recommendation**: Keep `VALIDATION_FAILED` in code. Update spec §7.4.12's
stable-codes list to read `VALIDATION_FAILED` in a future spec revision. Do not
touch the code — the string is baked into 9+ call sites and a clutch of tests.

**Action required**: User confirms direction (code wins vs spec wins) before
spec edit. Until then, treat `VALIDATION_FAILED` as the de-facto canonical
code and document the drift in the blueprint reconciliation note.

---

## D-2: Default SQLite path — `state.db` vs `amc.db` — **RESOLVED (code wins)**

Resolved during the AMC→AMG rebrand. The spec, `.env.example`, `backup.sh` and
every doc now say **`state.db`**, matching `amg/core/db.py` and
`amg/migrations/env.py`. Nothing had ever created an `amc.db`, so there was no
data to move and no operator-side `mv` to perform.

The divergence was not harmless, as the original note assumed. `.env.example`
shipped `AMC_DB_PATH=…/amc.db`, so an operator who copied it — as happened on
the development machine — ran the adapter against a file that did not exist
while Alembic migrated the `state.db` that did. `ops/scripts/backup.sh` had the
same default and would have silently backed up nothing.

Keeping the brand-free `state.db` is what let the rebrand leave the operator's
database, logs and secrets untouched under `~/…/messaging-agent/`.

**Operator action**: if your `.env` still sets `AMG_DB_PATH` to a path ending in
`amc.db`, delete the line and let the default resolve. See
[the runbook](../../docs/operations/runbook.md#migrating-from-amc-to-amg).

---

---

## D-3: MCP wrapper language — TypeScript → Python (resolved 2026-05-09)

**Resolution**: spec amended to Python. Not a divergence; recorded here for
audit only.

The spec's original Phase-3 language pick (`@modelcontextprotocol/sdk`,
TypeScript, Node 20+/Bun) was an early default rather than a load-bearing
constraint. v1.2 of the spec replaces it with the official `mcp` Python SDK
(FastMCP), and the implementation lives at `mcp/` as a uv workspace member.
Wrapper config (`AMG_BASE_URL` / `AMG_BEARER_TOKEN` / `AMG_AGENT_ID`),
the four-tool surface, the §7.4.12 error envelope, and the import-audit
contract are all unchanged. Operator setup loses its Node/Bun prerequisite.

Updated surfaces: spec §1, §5.6, §7.2 tech-stack rows, §7.4.7 phrasing,
§9.3 Phase-3 deliverables, §10.1 test-strategy rows, §10.7 R-9 risk row,
§15.2 glossary, §15.5 references, §15.4 changelog (1.2 row).

No code-side migration needed beyond replacing `mcp-wrapper/` with `mcp/`;
the four MCP tools and adapter REST endpoints are byte-identical.

---

## How to resolve

When ready to close these out:

1. Confirm direction per item (typical answer: "code wins, edit the spec").
2. Apply spec edits in a single commit referencing this notes file.
3. Move this file to `internal/notes/archive/` (or delete) and remove the
   reconciliation footnote from `internal/blueprints/agent-messaging-gateway.md`.
