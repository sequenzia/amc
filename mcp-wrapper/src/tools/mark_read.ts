// MCP tool: `mark_read`.
//
// Spec references:
//   - blueprint §6.1 — tool surface (`mark_read` input/output)
//   - blueprint §5.1 / §7.4.4 — `POST /messages/mark_read`
//   - §14 OQ-1 — `marked_count` is the count of unique ids submitted, not
//                the number of newly inserted rows (server-side semantics; the
//                wrapper just forwards what the adapter returns).
//
// Design contract:
//   * Self-contained module — exports a single `markRead` object with
//     { name, description, inputSchema, handler }. Registration with the MCP
//     server happens elsewhere (#65), not here.
//   * No platform-specific code (no discord/applescript/chat.db imports).
//   * Each `message_id` must match the spec ULID-prefixed shape:
//       msg_<26 chars from Crockford base32 (digits + uppercase A-Z minus I, L, O, U)>
//     This mirrors the regex used by `get_message_context` and the Python
//     `MessageId` annotated type in `amc/core/envelope.py`.
//   * The handler returns the raw `HttpResult<{marked_count}>` so the per-tool
//     error mapper added in #64 can translate non-2xx responses uniformly.
//   * `mark_read` is naturally idempotent at the storage layer (UPSERT on
//     `(message_id, agent_id)`) so we do NOT attach an `Idempotency-Key` —
//     re-issuing the call simply refreshes `read_at`.

import { z } from 'zod';

import type { HttpClient, HttpResult } from '../http.js';

/** Full message id pattern used across the spec: `msg_<ULID>`. */
const MESSAGE_ID = /^msg_[0-9A-HJKMNP-TV-Z]{26}$/;

/** Zod input schema. Exported so tests can exercise validation directly. */
export const markReadInputSchema = z.object({
  message_ids: z.array(z.string().regex(MESSAGE_ID)),
});

export type MarkReadInput = z.infer<typeof markReadInputSchema>;

/** Adapter response shape for `POST /messages/mark_read` (spec §7.4.4). */
export interface MarkReadOutput {
  readonly marked_count: number;
}

const TOOL_NAME = 'mark_read';
const TOOL_DESCRIPTION =
  'Acknowledge one or more messages so they no longer appear in ' +
  '`list_unread_messages`. Maps to POST /messages/mark_read. Returns ' +
  '`{ marked_count }`: the total number of unique ids submitted.';

export const markRead = {
  name: TOOL_NAME,
  description: TOOL_DESCRIPTION,
  inputSchema: markReadInputSchema,
  async handler(
    input: MarkReadInput,
    http: HttpClient,
  ): Promise<HttpResult<MarkReadOutput>> {
    // Re-validate at the boundary so direct programmatic callers (tests, the
    // future #65 registration wiring) get the same guarantees the MCP
    // framework provides for tool invocations.
    const parsed = markReadInputSchema.parse(input);
    return http.post<MarkReadOutput>('/messages/mark_read', parsed);
  },
} as const;

// Internal exports for unit tests.
export { MESSAGE_ID };
