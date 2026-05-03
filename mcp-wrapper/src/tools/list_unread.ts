// MCP tool: `list_unread_messages`.
//
// Spec references:
//   - blueprint §6.1 — tool surface (since / source / channel_id / limit)
//   - blueprint §5.1 — adapter REST: GET /messages/unread
//
// Design contract:
//   * Self-contained module — exports a single `listUnreadMessages` object
//     with { name, description, inputSchema, handler }. Registration with the
//     MCP server happens elsewhere (#65), not here.
//   * No platform-specific code (no discord/applescript/chat.db). The wrapper
//     is purely a transport-agnostic proxy to the adapter.
//   * Input is validated by the caller (the MCP framework runs the zod schema
//     before invoking `handler`), but the handler is also conservatively typed
//     so direct callers in tests get the same guarantees.
//   * Query params are built by omitting any undefined fields so the adapter
//     sees exactly what the agent supplied — never `?source=undefined`.
//   * The MCP `content` field follows the SDK convention: a single text block
//     containing the pretty-printed JSON `{ messages, next_since }` so an
//     agent reading the raw tool response gets a human-and-machine-friendly
//     payload. The structured payload is also exposed as `data` for callers
//     that prefer not to re-parse the text.

import { z } from 'zod';

import type { HttpClient, QueryParams } from '../http.js';

/** Allowlisted source identifiers; mirrors the adapter's `Source` enum. */
export const LIST_UNREAD_SOURCES = ['imessage', 'discord'] as const;

/** Zod input schema. Exported so tests can exercise validation directly. */
export const listUnreadInputSchema = z.object({
  since: z.string().optional(),
  source: z.enum(LIST_UNREAD_SOURCES).optional(),
  channel_id: z.string().optional(),
  limit: z.number().int().min(1).max(100).optional(),
});

export type ListUnreadInput = z.infer<typeof listUnreadInputSchema>;

/**
 * Shape of the adapter's `GET /messages/unread` response. Re-declared here
 * (rather than imported from a shared types module) to keep this tool
 * self-contained per the blueprint contract.
 */
export interface ListUnreadResponse {
  readonly messages: readonly unknown[];
  readonly next_since: string | null;
}

/** MCP tool result returned by the handler. */
export interface ListUnreadToolResult {
  readonly content: ReadonlyArray<{ type: 'text'; text: string }>;
  readonly data?: ListUnreadResponse;
  readonly isError?: boolean;
}

const TOOL_NAME = 'list_unread_messages';
const TOOL_DESCRIPTION =
  'Return allowlisted messages the agent has not yet acknowledged. ' +
  'Optional filters: source, channel_id, since (ISO 8601), limit.';

/**
 * Build the query param object for the adapter call. Omits any field that
 * the caller did not supply so the resulting URL has no spurious params.
 */
function buildQuery(input: ListUnreadInput): QueryParams {
  const query: Record<string, string | number> = {};
  if (input.since !== undefined) query.since = input.since;
  if (input.source !== undefined) query.source = input.source;
  if (input.channel_id !== undefined) query.channel_id = input.channel_id;
  if (input.limit !== undefined) query.limit = input.limit;
  return query;
}

/**
 * Translate a successful adapter payload into the MCP tool result envelope.
 * The payload is normalized so `next_since` is always present (null when the
 * adapter omits it) — this keeps the agent's downstream code simple.
 */
function toToolResult(raw: unknown): ListUnreadToolResult {
  const data = normalizeResponse(raw);
  return {
    data,
    content: [
      {
        type: 'text',
        text: JSON.stringify(data, null, 2),
      },
    ],
  };
}

function normalizeResponse(raw: unknown): ListUnreadResponse {
  if (raw === null || typeof raw !== 'object') {
    return { messages: [], next_since: null };
  }
  const obj = raw as { messages?: unknown; next_since?: unknown };
  const messages = Array.isArray(obj.messages) ? obj.messages : [];
  const nextSince =
    typeof obj.next_since === 'string' ? obj.next_since : obj.next_since === null ? null : null;
  return { messages, next_since: nextSince };
}

/**
 * Translate an HTTP error result from the adapter into an MCP tool result
 * with `isError: true` and a structured text body. Agents see the spec
 * §7.4.12 envelope verbatim.
 */
function toErrorResult(code: string, message: string, status: number): ListUnreadToolResult {
  const body = { error: { code, message, status } };
  return {
    isError: true,
    content: [
      {
        type: 'text',
        text: JSON.stringify(body, null, 2),
      },
    ],
  };
}

export const listUnreadMessages = {
  name: TOOL_NAME,
  description: TOOL_DESCRIPTION,
  inputSchema: listUnreadInputSchema,
  async handler(input: ListUnreadInput, http: HttpClient): Promise<ListUnreadToolResult> {
    // Re-validate at the boundary so direct programmatic callers (tests, the
    // future #65 registration wiring) get the same guarantees the MCP
    // framework provides for tool invocations.
    const parsed = listUnreadInputSchema.parse(input);
    const result = await http.get<unknown>('/messages/unread', buildQuery(parsed));
    if (!result.ok) {
      return toErrorResult(result.code, result.message, result.status);
    }
    return toToolResult(result.data);
  },
} as const;
