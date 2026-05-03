// MCP tool: `get_message_context`
//
// Spec references:
//   - §5.4 — feature definition (US-004) and acceptance criteria
//   - §7.4.3 — `GET /messages/context?channel_id=&around_message_id=&before=&after=`
//   - §7.4.7 — MCP tool surface (this is one of the four)
//
// Design contract:
//   * Pure shim over the adapter HTTP API. Does not interpret the response —
//     just forwards `{messages}` after validating the input shape.
//   * Input validation happens via zod; defaults (`before=5`, `after=5`) and
//     bounds (`<=50`) are enforced here so we fail fast with a useful Zod
//     error instead of waiting for the adapter to return 422.
//   * The HTTP client is injected so this handler is testable without any
//     network or config.
//   * No platform-specific imports (per §9.3 / no-platform-imports.test.ts).
//
// `around_message_id` regex matches the spec's ULID shape: `msg_` followed by
// 26 Crockford base32 chars (digits + uppercase A-Z minus I, L, O, U).

import { z } from 'zod';

import type { HttpClient, HttpResult } from '../http.js';

/** ULID body: 26 chars from Crockford base32 (no I, L, O, U). */
const ULID_BODY = /^[0-9A-HJKMNP-TV-Z]{26}$/;

/** Full message id pattern used across the spec: `msg_<ULID>`. */
const MESSAGE_ID = /^msg_[0-9A-HJKMNP-TV-Z]{26}$/;

/**
 * Input schema for `get_message_context`. Defaults and caps mirror §5.4 and
 * §7.4.3 — `before`/`after` default to 5 and are capped at 50.
 */
export const getMessageContextInput = z.object({
  channel_id: z.string(),
  around_message_id: z.string().regex(MESSAGE_ID),
  before: z.number().int().min(0).max(50).default(5),
  after: z.number().int().min(0).max(50).default(5),
});

export type GetMessageContextInput = z.infer<typeof getMessageContextInput>;

/** Response shape. We don't validate the envelope contents — that's the adapter's job. */
export interface GetMessageContextOutput {
  readonly messages: unknown[];
}

const DESCRIPTION =
  'Fetch N messages before and after a target message in a channel, in chronological order. ' +
  'Maps to GET /messages/context. Defaults: before=5, after=5. Caps: before<=50, after<=50.';

/**
 * Tool handler. Takes parsed input plus an injected `HttpClient` and returns
 * the raw `HttpResult` so the caller (registered MCP server) can map errors
 * uniformly using the shared error-mapping helper added in #64.
 */
async function handler(
  input: GetMessageContextInput,
  http: HttpClient,
): Promise<HttpResult<GetMessageContextOutput>> {
  const parsed = getMessageContextInput.parse(input);
  return http.get<GetMessageContextOutput>('/messages/context', {
    channel_id: parsed.channel_id,
    around_message_id: parsed.around_message_id,
    before: parsed.before,
    after: parsed.after,
  });
}

/**
 * Exported tool descriptor. The MCP wrapper entrypoint (added when the four
 * tools are wired up) imports this and registers it with the server.
 */
export const getMessageContext = {
  name: 'get_message_context' as const,
  description: DESCRIPTION,
  inputSchema: getMessageContextInput,
  handler,
};

export type GetMessageContextTool = typeof getMessageContext;

// Internal exports for unit tests.
export { ULID_BODY, MESSAGE_ID };
