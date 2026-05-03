# Spec vs Code Divergences (Phase 1 Reconciliation)

**Status**: Open — needs user decision
**Date**: 2026-05-03
**Discovered during**: Task #45 (blueprint reconciliation, end of Phase 1)
**Related**: spec §7.4.12, spec §11.2

---

## Purpose

Tracks wording / value mismatches between `specs/agent-messaging-channel-SPEC.md` v1.1
and the in-tree implementation found at end of Phase 1. Each item below needs the
user to pick the canonical form. Until that happens, the blueprint reconciliation
note (`internal/blueprints/agent-messaging-channel.md`, end of file) records the
drift but does **not** make the call.

The recommendation in every case below is "code wins, update the spec later" —
the code is what tests run against, and changing it now risks breaking 168 tests
of in-flight work for cosmetic reasons.

---

## D-1: Validation error code — `VALIDATION_FAILED` vs `VALIDATION_ERROR`

| Surface | Value | Locations |
|---------|-------|-----------|
| Code (canonical in tree) | `VALIDATION_FAILED` | `amc/core/errors.py:70`, used by `amc/api/messages_unread.py`, `amc/api/messages_quarantine.py`, `amc/api/messages_context.py` |
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

## D-2: Default SQLite path — `state.db` vs `amc.db`

| Surface | Value | Locations |
|---------|-------|-----------|
| Code (canonical in tree) | `~/Library/Application Support/messaging-agent/state.db` | `amc/core/db.py:55`, `amc/migrations/env.py:33` |
| Spec §11.2 (env var table) | `~/Library/Application Support/messaging-agent/amc.db` | spec line 1444 |

`state.db` is the historical name carried over from earlier task drafts; `amc.db`
is the spec's later choice (per the project name). Neither file exists yet —
nothing is broken, but the default the code installs to differs from the default
the spec advertises in `SETUP.md`-equivalent material.

The env var name `AMC_DB_PATH` is identical in both surfaces. Operators who set
the env var explicitly are unaffected. The drift only matters for anyone
reading the spec to find out where the file landed by default.

`amc/core/db.py:23-26` already calls this drift out in a docstring.

**Recommendation**: Keep `state.db` in code (do not rename — would require an
operator-side `mv` for any deployment that already installed under the old name,
plus updates to migrations/env.py and any docs that quote the path). Update
spec §11.2's `AMC_DB_PATH` row to read `state.db` in a future spec revision.

**Action required**: User confirms direction (code wins vs spec wins) before
spec edit. Until then, treat `state.db` as the de-facto canonical default and
document the drift in the blueprint reconciliation note.

---

## How to resolve

When ready to close these out:

1. Confirm direction per item (typical answer: "code wins, edit the spec").
2. Apply spec edits in a single commit referencing this notes file.
3. Move this file to `internal/notes/archive/` (or delete) and remove the
   reconciliation footnote from `internal/blueprints/agent-messaging-channel.md`.
