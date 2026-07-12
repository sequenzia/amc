# OQ-1 Decision: `marked_count` semantics for `POST /messages/mark_read`

**Status**: Resolved
**Date**: 2026-05-03
**Section**: spec §7.4.4, §14 OQ-1
**Decided by**: implementation of task #33

---

## Open Question (verbatim from spec §14)

> Should `mark_read` return `marked_count` = total IDs marked, or only IDs newly marked in this call?

## Decision

**`marked_count` = total number of unique IDs the client submitted in the request body** (after deduplication), NOT the number of newly inserted rows.

## Reasoning

### 1. Matches the spec example (§7.4.4)

The spec shows:

```http
POST /messages/mark_read
{ "message_ids": ["msg_01HXYZ...", "msg_01HABC..."] }
```

with response:

```json
{ "marked_count": 2 }
```

Two ids in, `marked_count: 2` out. The example is deterministic: the response equals the request size. Picking "newly inserted rows" would make the example wrong on any replay (second call would return `marked_count: 0`).

### 2. Aligns with the §7.3 example table

The §7.3 acceptance table includes:

> Same agent marks same message read twice → Two `mark_read` calls with same `[X]` → UPSERT is a no-op on second call; both return `marked_count: 1`

This explicitly states the second call returns `marked_count: 1`, not `0`. Only the "submitted IDs" semantic produces this result.

### 3. Idempotency at the API surface

The HTTP contract is "I asked you to mark these N ids; you marked them." From the client's perspective, a `200 OK` with `marked_count == len(message_ids)` is the success signal. Returning a varying lower count on retry would force every caller to compare their input length against the response, defeating the point of UPSERT idempotency.

### 4. Non-existent IDs

A side effect of this choice: if the client submits an id that does not exist in the `messages` table, the FK-protected insert into `message_reads` silently fails (we swallow the error per OQ-1) but the id is **still counted** in `marked_count`. The API cannot distinguish "you sent an id I don't know about" from "you sent an id I do know about" without an extra round trip, and surfacing the discrepancy would leak storage state.

If a caller needs to verify which ids actually got persisted, they can query `GET /messages/unread` afterwards — any id that still appears was not successfully marked.

### 5. Deduplication

If the client submits the same id twice in one call, the server deduplicates before counting. This prevents trivial inflation (`["msg_x", "msg_x"]` returns `marked_count: 1`, not `2`).

## Trade-off considered and rejected

**"Newly inserted rows only"** would be useful for callers building progress UIs ("you marked 5 NEW messages"). But:

- It would contradict the spec example.
- It would require either (a) pre-checking existence before insert (extra query) or (b) inspecting `INSERT ... ON CONFLICT` row counts (driver-specific, not portable across SQLite vs. future Postgres).
- It would make the response value race-y under concurrent agents — agent A's mark could arrive between agent B's "is it new?" check and B's UPSERT, causing B to under-count.

## Implementation summary

- Endpoint: `amg/api/messages_mark_read.py`
- `marked_count = len(set(payload.message_ids))` after deduplication
- Per-id UPSERT wrapped in a savepoint so FK violations do not poison the batch
- Empty `message_ids` returns `{ "marked_count": 0 }` without touching the DB
- Test coverage: see `tests/api/test_messages_mark_read.py`
