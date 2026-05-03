// Unit tests for src/tools/mark_read.ts.
//
// Mirrors the pattern used in tests/tools/send.test.ts and tests/http.test.ts:
// stub `fetch` via a recorder so the handler runs end-to-end through the real
// `HttpClient` without touching the network. We assert:
//   * Tool metadata (name, description, schema shape).
//   * Input validation: message_ids is required, each id must match the
//     spec ULID pattern, an empty array is accepted (the adapter resolves it
//     to `marked_count: 0`).
//   * The handler issues `POST /messages/mark_read` with the request body and
//     does NOT attach an `Idempotency-Key` (the operation is idempotent at the
//     storage layer per spec §7.4.4).
//   * The handler returns `{ marked_count }` on 2xx and a structured `HttpErr`
//     on non-2xx / network failures.

/* eslint-disable no-undef */

import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WrapperConfig } from '../../src/config.js';
import { createHttpClient } from '../../src/http.js';
import { MESSAGE_ID, markRead, markReadInputSchema } from '../../src/tools/mark_read.js';

interface RecordedCall {
  url: string;
  init: RequestInit;
}

const TEST_CONFIG: WrapperConfig = {
  baseUrl: 'http://127.0.0.1:8080',
  bearerToken: 'test-token',
  agentId: 'claude-code',
};

// Two valid spec-shaped ids (msg_ + 26 Crockford base32 chars; Crockford
// excludes I, L, O, U).
const VALID_ID_A = 'msg_01HXYZ123456789ABCDEFGHJK0';
const VALID_ID_B = 'msg_01HABC456789ABCDEFGHJKMNP0';

function makeFetchStub(responder: (call: RecordedCall) => Response | Promise<Response>): {
  fetchImpl: typeof fetch;
  calls: RecordedCall[];
} {
  const calls: RecordedCall[] = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const recorded: RecordedCall = { url, init: init ?? {} };
    calls.push(recorded);
    return responder(recorded);
  }) as unknown as typeof fetch;
  return { fetchImpl, calls };
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

function headerOf(init: RequestInit, name: string): string | undefined {
  const h = init.headers as Record<string, string> | undefined;
  if (!h) return undefined;
  for (const [k, v] of Object.entries(h)) {
    if (k.toLowerCase() === name.toLowerCase()) return v;
  }
  return undefined;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('markRead tool metadata', () => {
  it('declares the canonical tool name', () => {
    expect(markRead.name).toBe('mark_read');
  });

  it('exposes a non-empty description', () => {
    expect(typeof markRead.description).toBe('string');
    expect(markRead.description.length).toBeGreaterThan(0);
  });

  it('input schema accepts a non-empty array of spec-shaped ids', () => {
    const parsed = markReadInputSchema.parse({
      message_ids: [VALID_ID_A, VALID_ID_B],
    });
    expect(parsed.message_ids).toEqual([VALID_ID_A, VALID_ID_B]);
  });

  it('input schema accepts an empty array (the adapter resolves to marked_count: 0)', () => {
    // Empty list is structurally valid; servers may treat it as a no-op. The
    // wrapper does not opinion this — it forwards to the adapter.
    const parsed = markReadInputSchema.parse({ message_ids: [] });
    expect(parsed.message_ids).toEqual([]);
  });

  it('input schema rejects when message_ids is missing', () => {
    expect(() => markReadInputSchema.parse({})).toThrow();
  });

  it('input schema rejects when message_ids is not an array', () => {
    expect(() =>
      markReadInputSchema.parse({ message_ids: VALID_ID_A } as unknown),
    ).toThrow();
  });

  it('input schema rejects ids missing the `msg_` prefix', () => {
    // Same 26-char body as VALID_ID_A — only the prefix is wrong.
    expect(() =>
      markReadInputSchema.parse({
        message_ids: ['01HXYZ123456789ABCDEFGHJK0'],
      }),
    ).toThrow();
  });

  it('input schema rejects ids whose ULID body uses a forbidden character', () => {
    // Crockford base32 excludes I, L, O, U — the `O` here must fail.
    expect(() =>
      markReadInputSchema.parse({
        message_ids: ['msg_01HXYZ123456789ABCDEFGHJO0'],
      }),
    ).toThrow();
  });

  it('input schema rejects ids with the wrong ULID length', () => {
    expect(() =>
      markReadInputSchema.parse({
        message_ids: ['msg_01HXYZ'],
      }),
    ).toThrow();
  });

  it('input schema rejects ids that are not strings', () => {
    expect(() =>
      markReadInputSchema.parse({ message_ids: [123 as unknown as string] }),
    ).toThrow();
  });

  it('input schema rejects when even one id in a batch is malformed', () => {
    expect(() =>
      markReadInputSchema.parse({
        message_ids: [VALID_ID_A, 'not-a-message-id'],
      }),
    ).toThrow();
  });
});

describe('MESSAGE_ID regex', () => {
  it('matches the canonical spec shape', () => {
    expect(MESSAGE_ID.test(VALID_ID_A)).toBe(true);
    expect(MESSAGE_ID.test(VALID_ID_B)).toBe(true);
  });

  it('rejects ids with lowercase ULID body', () => {
    expect(MESSAGE_ID.test('msg_01hxyz123456789abcdefghjk0')).toBe(false);
  });

  it('rejects ids with the wrong prefix', () => {
    expect(MESSAGE_ID.test('att_01HXYZ123456789ABCDEFGHJK0')).toBe(false);
  });
});

describe('markRead.handler', () => {
  it('POSTs to /messages/mark_read with the request body', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({ marked_count: 2 }));
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await markRead.handler(
      { message_ids: [VALID_ID_A, VALID_ID_B] },
      http,
    );

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.status).toBe(200);
      expect(result.data).toEqual({ marked_count: 2 });
    }

    expect(calls).toHaveLength(1);
    const call = calls[0]!;
    expect(call.url).toBe('http://127.0.0.1:8080/messages/mark_read');
    expect(call.init.method).toBe('POST');
    expect(call.init.body).toBe(
      JSON.stringify({ message_ids: [VALID_ID_A, VALID_ID_B] }),
    );
  });

  it('does NOT attach an Idempotency-Key header (mark_read is naturally idempotent)', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({ marked_count: 1 }));
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await markRead.handler({ message_ids: [VALID_ID_A] }, http);

    expect(headerOf(calls[0]!.init, 'Idempotency-Key')).toBeUndefined();
  });

  it('sends default Authorization, X-Agent-ID, and Content-Type headers', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({ marked_count: 1 }));
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await markRead.handler({ message_ids: [VALID_ID_A] }, http);

    expect(headerOf(calls[0]!.init, 'Authorization')).toBe('Bearer test-token');
    expect(headerOf(calls[0]!.init, 'X-Agent-ID')).toBe('claude-code');
    expect(headerOf(calls[0]!.init, 'Content-Type')).toBe('application/json');
  });

  it('forwards an empty array verbatim and surfaces marked_count: 0', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({ marked_count: 0 }));
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await markRead.handler({ message_ids: [] }, http);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data).toEqual({ marked_count: 0 });
    }
    expect(calls[0]!.init.body).toBe(JSON.stringify({ message_ids: [] }));
  });

  it('returns the structured HttpErr on a non-2xx response', async () => {
    const errorBody = {
      error: { code: 'NOT_FOUND', message: 'No such message' },
    };
    const { fetchImpl } = makeFetchStub(
      () =>
        new Response(JSON.stringify(errorBody), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await markRead.handler({ message_ids: [VALID_ID_A] }, http);

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.status).toBe(404);
      expect(result.code).toBe('NOT_FOUND');
      expect(result.message).toBe('No such message');
      expect(result.body).toEqual(errorBody);
    }
  });

  it('returns NETWORK_ERROR when fetch rejects', async () => {
    const fetchImpl = (async () => {
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await markRead.handler({ message_ids: [VALID_ID_A] }, http);

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('NETWORK_ERROR');
    }
  });

  it('rejects invalid input via zod before issuing any HTTP call', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({ marked_count: 0 }));
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await expect(
      markRead.handler(
        { message_ids: ['not-a-message-id'] } as unknown as Parameters<
          typeof markRead.handler
        >[0],
        http,
      ),
    ).rejects.toThrow();

    expect(calls).toHaveLength(0);
  });
});
