// HTTP -> MCP error mapping.
//
// Spec references:
//   - §7.4.12 — adapter error envelope: { error: { code, message, details? } }
//   - §6.1   — MCP tool surface (each tool returns either a normal result or
//              an `{ content: [...], isError: true }` payload)
//
// Design contract:
//   * Input is the `HttpErr` discriminant returned by `src/http.ts`. The
//     mapper never throws and never inspects request state — it is a pure
//     function from a known error shape to an MCP `isError` content payload.
//   * Each known adapter error code maps to one short human-readable line. The
//     mapping is intentionally explicit (not a template) so reviewers can see
//     every exposed string in one place.
//   * Unknown codes fall back to the generic INTERNAL_ERROR message, embedding
//     the upstream `message` so the agent still has something actionable.
//   * Network failures (no HTTP response at all) include the configured base
//     URL so the agent can suggest a concrete fix.
//
// The mapper is consumed by every tool handler (#60-#63): on a non-ok
// `HttpResult` they pass it through `mapHttpErrorToMcpResponse` and return the
// resulting payload directly to the MCP runtime.

import { getConfig } from './config.js';
import type { HttpErr } from './http.js';

/**
 * MCP tool error response. Mirrors the shape the @modelcontextprotocol/sdk
 * runtime expects from a tool handler when the call did not succeed.
 */
export interface McpErrorResponse {
  readonly content: ReadonlyArray<{ readonly type: 'text'; readonly text: string }>;
  readonly isError: true;
}

/**
 * Optional override hook for tests that want to assert the network-error
 * message without booting the full config singleton.
 */
export interface MapErrorOptions {
  /** Resolve the adapter base URL. Defaults to `getConfig().baseUrl`. */
  resolveBaseUrl?: () => string;
}

/**
 * Translate an `HttpErr` into the MCP tool error response shape.
 *
 * The mapping is keyed primarily on the upstream `code` (from spec §7.4.12).
 * Status code is used as a sanity hint and as a fallback for unmapped codes.
 */
export function mapHttpErrorToMcpResponse(
  error: HttpErr,
  options: MapErrorOptions = {},
): McpErrorResponse {
  const text = renderErrorText(error, options);
  return {
    content: [{ type: 'text', text }],
    isError: true,
  };
}

function renderErrorText(error: HttpErr, options: MapErrorOptions): string {
  // Network-level failure: no HTTP response, so we report the unreachable URL.
  if (error.code === 'NETWORK_ERROR') {
    const baseUrl = safeBaseUrl(options);
    return `Cannot reach adapter at ${baseUrl}. Is it running?`;
  }

  switch (error.code) {
    case 'UNAUTHORIZED':
      return 'Bearer token rejected by adapter. Check AMC_BEARER_TOKEN.';

    case 'AGENT_ID_REQUIRED':
      return 'Internal error: agent id missing — please report.';

    case 'MESSAGE_NOT_FOUND':
      return 'Message not found or quarantined.';

    case 'CHANNEL_NOT_FOUND':
      return (
        'Channel not registered. Send to a channel that has received at ' +
        'least one inbound message.'
      );

    case 'IDEMPOTENCY_KEY_REUSE':
      return 'Internal error: idempotency key collision — please retry.';

    case 'RATE_LIMITED': {
      const retryAfter = extractRetryAfter(error);
      const retryStr = retryAfter ?? 'a few';
      return `Rate limited. Retry after ${retryStr} seconds.`;
    }

    case 'PLATFORM_SEND_FAILED':
    case 'SEND_FAILED':
      return `Platform delivery failed: ${error.message}.`;

    case 'PLATFORM_AUTH':
      return 'Platform authentication failed (e.g., Discord token rejected).';

    case 'ATTACHMENT_TOO_LARGE_FOR_PLATFORM':
      return 'Attachment exceeds platform limit. Compress or omit.';

    case 'INTERNAL_ERROR':
    default:
      return `Adapter internal error: ${error.message}.`;
  }
}

/**
 * Pull the `Retry-After` value out of the §7.4.12 error envelope's `details`.
 * The adapter conventionally surfaces it as `details.retry_after` (numeric
 * seconds). String values are also accepted so a future header pass-through
 * keeps working.
 */
function extractRetryAfter(error: HttpErr): string | null {
  const body = error.body;
  if (body === null || body === undefined || typeof body !== 'object') {
    return null;
  }
  const errField = (body as { error?: unknown }).error;
  if (errField === null || errField === undefined || typeof errField !== 'object') {
    return null;
  }
  const details = (errField as { details?: unknown }).details;
  if (details === null || details === undefined || typeof details !== 'object') {
    return null;
  }
  const raw = (details as Record<string, unknown>)['retry_after'];
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return String(raw);
  }
  if (typeof raw === 'string' && raw.trim().length > 0) {
    return raw.trim();
  }
  return null;
}

/**
 * Resolve the configured base URL without crashing if `loadConfig()` was never
 * called (e.g. in a unit test that only exercises the mapper). Falls back to a
 * generic placeholder so the message is still readable.
 */
function safeBaseUrl(options: MapErrorOptions): string {
  if (options.resolveBaseUrl) {
    try {
      return options.resolveBaseUrl();
    } catch {
      return 'AMC_BASE_URL';
    }
  }
  try {
    return getConfig().baseUrl;
  } catch {
    return 'AMC_BASE_URL';
  }
}
