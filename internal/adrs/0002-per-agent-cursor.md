# ADR 0002: Per-agent read state via `message_reads` join table

**Status**: Accepted
**Date**: 2026-05-03

## Context

The blueprint originally modeled "read" as a `read_at TIMESTAMP` column directly on the `messages` table (blueprint §5.3, pre-reconciliation). This made `mark_read` a single `UPDATE messages SET read_at = ?` and `list_unread_messages` a single `WHERE read_at IS NULL` — minimum schema, minimum query.

Two forces broke that simplicity during Phase 1 design:

1. **REQ-AMC-003 (per-agent cursors).** The spec requires that two MCP-using agents polling the same adapter each see their own independent unread queue. With a single `read_at` column, whichever agent calls `mark_read` first silently marks the message read for **every** other agent — there is no per-agent dimension to that column.
2. **`X-Agent-ID` is already a first-class header on every MCP request** (§7.4.2). The adapter can identify the calling agent for free at request time. There is no privilege barrier to maintaining per-agent state — it just needs a place to live.

Two viable designs exist:

- **Schema A: keep `read_at` on `messages`, add an `agent_id` column.** Means a message can be in only one (message, agent) row, which is wrong: every agent that ever sees the message needs its own row. Effectively this collapses to Schema B with extra steps and a redundant `messages` row per agent.
- **Schema B: remove `read_at` from `messages`; add a `message_reads(message_id, agent_id, read_at)` join table with composite PK.** Each `(message_id, agent_id)` row records that one specific agent has read one specific message. The unread query for agent `A` becomes a `LEFT JOIN ... WHERE agent_id = A AND message_reads.read_at IS NULL`.

A third "leasing" model (§9 of the blueprint, original third bullet) was also on the table: agents lease a batch of message IDs from a queue, and unleased messages are visible to anyone. Rejected because it requires a lease-expiry sweeper, complicates retries, and produces head-of-line blocking when one agent crashes mid-lease.

## Decision

Adopt **Schema B**:

- Remove `read_at` from `messages`. Reads are not a property of the message.
- Add a new `message_reads(message_id, agent_id, read_at)` table with composite primary key `(message_id, agent_id)` and a foreign key `message_id → messages(id)`.
- The unread query for agent `A` reads:

  ```sql
  SELECT m.*
  FROM messages m
  LEFT JOIN message_reads mr
    ON mr.message_id = m.id AND mr.agent_id = :agent_id
  WHERE mr.read_at IS NULL
    AND m.allowlist_status = 'allowed'
  ORDER BY m.created_at;
  ```

- `mark_read` UPSERTs one row per submitted ID into `message_reads` keyed by `(message_id, X-Agent-ID)`. Re-marking is a no-op (UPSERT semantics).
- The cursor (next-since timestamp) returned by `list_unread_messages` is also per-agent: it is derived from the requesting agent's most-recent `read_at`, not from any global watermark.

The blueprint's §5.3 has been reconciled to this shape; this ADR captures the rationale.

## Consequences

### Positive

- **No cross-agent contamination.** Two agents can call `list_unread_messages` and `mark_read` against the same message in any order; each only mutates its own row in `message_reads`.
- **`mark_read` is naturally idempotent.** Per OQ-1 (resolved in `internal/notes/oq-1-decision.md`), `marked_count` returns the size of the deduped input set; UPSERT semantics make replay safe and deterministic.
- **The `messages` table is append-only with respect to read state.** Nothing about a message changes after it is inserted by a connector — easier reasoning, friendlier to future replication or read replicas.
- **Future "agent groups" or "tenant isolation" scenarios fall out naturally.** Want one shared inbox across two agents? Make them share an `agent_id`. Want strict isolation? Don't.

### Negative

- **One extra table to migrate, index, and back up.** The composite PK on `(message_id, agent_id)` plus the FK to `messages(id)` covers the lookup pattern; no extra indexes required for v1.
- **The unread query is a `LEFT JOIN` instead of a column predicate.** Slightly more expensive at scale; at the personal-scale traffic this system targets (a few hundred messages/day per agent), the cost is unmeasurable.
- **The "global read state" concept no longer exists.** Tooling that wants "has any agent read this?" must aggregate across `message_reads` rows. Acceptable: no v1 surface needs that view.

### Neutral

- The schema can be inverted later (`message_reads` → materialized view of an event log) without changing the API surface, because the contract `list_unread_messages` / `mark_read` exposes is per-agent already.

## Alternatives considered

- **Single `read_at` column on `messages` (original blueprint).** Rejected — fails REQ-AMC-003 outright. Only works for a single-agent deployment.
- **`agent_id` column added to `messages`.** Rejected — collapses to N copies of every message row, one per agent that has seen it. Worse than Schema B at every dimension.
- **Leasing model.** Rejected — adds a sweeper, expiry semantics, and head-of-line blocking. Solves a problem (cooperative work distribution) AMC v1 does not have.
- **Bitmap-per-message.** A `read_by BIGINT` bitmap with one bit per registered agent would compact storage, but requires an agent registry and breaks the "agents are anonymous, identified only by header" contract. Rejected.

## References

- Blueprint §5.3 — reconciled storage schema
- Blueprint §9 — the (now-resolved) "Multi-agent contention" open question
- Spec §5.3 — REQ-AMC-003 (read state per agent)
- Spec §7.3.2 / §7.3.3 — `messages` and `message_reads` table definitions
- `internal/notes/oq-1-decision.md` — `marked_count` semantics on `mark_read`
- ADR 0006 — MCP stdio-only (means the typical deployment is one agent per stdio child, but the schema does not assume that)
