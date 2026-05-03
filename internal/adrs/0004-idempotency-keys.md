# ADR 0004: Client-supplied Idempotency-Key with 24 h cache and body-hash collision detection

**Status**: Accepted
**Date**: 2026-05-03

## Context

`POST /messages/send` is the only non-idempotent write surface in the adapter — every call results in a new outbound message on a real platform. Network and process failure modes that retry the same logical send produce duplicate outbound messages unless the adapter dedupes:

- An MCP client retries because the adapter response was lost in transit but the side effect already happened.
- An MCP client crashes after queuing the request but before reading the response.
- A Phase 4 webhook receiver triggers a "re-send on failure" loop in the agent runtime.

`POST /messages/mark_read` is naturally idempotent under the per-agent UPSERT design (ADR 0002) and does not need this mechanism. `POST /typing` is fire-and-forget. So the entire problem is scoped to `/messages/send`.

The standard solution is the `Idempotency-Key` header pattern — a client-supplied unique value that the server uses to dedupe. Three design points must be pinned:

1. **Who picks the key?** Spec §5.2 already mandates it is **client-supplied**, formatted as a UUID. The MCP wrapper auto-generates one per `send_message` tool call.
2. **How long is a cached response valid?** Long enough to cover any plausible retry window for a single logical operation; short enough to bound storage growth. Spec §5.2 settled on **24 hours**.
3. **What happens on key collision with a different body?** Two interpretations: (a) treat the new body as a new request (silent override — dangerous), (b) reject as a client bug. OQ-5 (still-open in spec §14, leaning toward 422) was resolved at this ADR's writing in favor of rejection.

## Decision

`POST /messages/send` adopts a **client-supplied Idempotency-Key with body-hash collision detection and a 24 h cache**:

- Clients SHOULD send `Idempotency-Key: <uuid>` on every `POST /messages/send`. (Recommended, not required, in spec §7.4.6 — but the MCP wrapper always sets one per OQ-5's resolution.)
- The adapter persists `(idempotency_key, agent_id, request_body_hash, response_body, created_at)` in an `idempotency_keys` table (spec §7.3.3) when it processes a fresh key.
- On a subsequent `POST /messages/send` with the same key:
  - If `request_body_hash` matches the cached row → return the **cached response** verbatim, with header `Idempotency-Replayed: true`. No platform call is made.
  - If `request_body_hash` does **not** match → return `422 Unprocessable Entity` with `code=IDEMPOTENCY_KEY_REUSE` and a message indicating the key was used with a different body.
- Cached rows are evicted **24 hours** after `created_at`. After eviction, the same key+body is treated as a fresh request and will re-send.
- The hash is an opaque digest of the canonicalized request body (e.g., `hashlib.sha256` over the JSON with sorted keys). Hash details are an implementation choice, not contractual; only the comparison semantics matter.

## Consequences

### Positive

- **Safe retries.** A client that loses a response can retry with the same key and either receive the cached response (success) or a deterministic 422 (the client itself sent two different bodies under one key — that is a client bug).
- **No silent duplicate sends.** The default failure mode of "the agent retried because the network glitched" never produces two iMessages or two Discord messages. This is the whole point of the mechanism.
- **No silent overrides.** A 422 on body mismatch surfaces a real client bug instead of papering over it. This is the OQ-5 stance: collision is implausible at scale (UUIDv4 over 24 h ≈ astronomically rare), so when it happens it is almost certainly the client reusing a key by accident.
- **Per-agent scoping reduces contention.** Two agents that happen to pick the same UUID (already implausible, but) cannot collide because the cache key is `(idempotency_key, agent_id)`. The `X-Agent-ID` header from the MCP wrapper is the natural scope.
- **Bounded storage growth.** 24 h eviction caps the table. At v1 traffic levels (a few hundred sends/day), the table holds at most a few thousand rows.

### Negative

- **One extra DB write per send.** Acceptable; sends are not the hot path.
- **Sweeper required.** Either a periodic cleanup task or a "delete on read if expired" policy. v1 ships a periodic sweeper; the time-injection pattern from `amc/core/rate_limit.py` is reused so the sweeper is testable without `time.sleep`.
- **Replay returns the original `sent_at`.** A client that uses `sent_at` to measure latency from its own send-call may be surprised by a "stale" timestamp on a replay. Documented in the spec §7.4.6 example.
- **Expired-then-re-sent edge case.** A client that retries 24 h + ε after the original send will perform a second platform send. Considered acceptable: 24 h is far longer than any reasonable retry budget, and the alternative (longer cache, unbounded growth) is worse.

### Neutral

- The `Idempotency-Replayed: true` response header is the contract for "this was a replay" and is the only signal a client gets that no second platform send happened. Clients that don't care about distinguishing replays from first-time sends can ignore it.

## Alternatives considered

- **No idempotency mechanism; rely on clients to dedupe.** Rejected — the cost (duplicate messages on real platforms in real conversations) is too high, and pushing this onto every client violates the principle that the adapter is the source of truth.
- **Server-generated idempotency keys.** Rejected because the keys must travel with the request to be useful; a server-generated key arrives in the *response*, after the side effect has already happened.
- **Cache forever (never evict).** Rejected — unbounded growth, no real upside vs. a 24 h window.
- **Cache for ≪ 24 h (e.g., 5 min).** Rejected as too short to cover client crash + restart + retry on a slow handoff. 24 h is a comfortable margin.
- **Silent override on key+body mismatch.** Rejected per OQ-5 reasoning: collision is overwhelmingly likely to indicate a client bug, and silently re-sending with a new body is the worst possible failure mode.

## References

- Blueprint §5.1 — `POST /messages/send` endpoint
- Spec §5.2 / REQ-AMC-002 — Outbound message sending feature
- Spec §7.3.3 — `idempotency_keys` table definition
- Spec §7.4.6 — `POST /messages/send` endpoint contract (Idempotency-Key, replay header, 422 code)
- Spec §14 OQ-5 — Idempotency-Key collision question (this ADR resolves it)
- Spec §15 — `IDEMPOTENCY_KEY_REUSE` error code
- ADR 0006 — MCP stdio-only (the wrapper auto-generates a key per `send_message` call)
