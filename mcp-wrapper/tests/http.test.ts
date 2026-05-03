// Unit tests for src/http.ts.
//
// We stub `globalThis.fetch` via vitest's `vi.stubGlobal` so the client never
// touches the network. Each test asserts on the recorded fetch call (URL,
// method, headers, body) and the shape of the returned HttpResult.
//
// `Response` is a Node 20+ global used to build mock responses. The flat
// eslint config doesn't list it under `globals`, so we silence `no-undef`
// for this file. (TypeScript's own checker still validates these names.)

/* eslint-disable no-undef */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WrapperConfig } from '../src/config.js';
import {
  NETWORK_ERROR_STATUS,
  createHttpClient,
  randomIdempotencyKey,
} from '../src/http.js';

interface RecordedCall {
  url: string;
  init: RequestInit;
}

const TEST_CONFIG: WrapperConfig = {
  baseUrl: 'http://127.0.0.1:8080',
  bearerToken: 'test-token',
  agentId: 'claude-code',
};

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
  // Header name comparison is case-insensitive; we set known casing.
  for (const [k, v] of Object.entries(h)) {
    if (k.toLowerCase() === name.toLowerCase()) return v;
  }
  return undefined;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('createHttpClient.get', () => {
  it('sends GET with default headers and parses JSON', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({ messages: [], next_since: null }),
    );
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.get<{ messages: unknown[] }>('/messages/unread');

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.status).toBe(200);
      expect(result.data).toEqual({ messages: [], next_since: null });
    }
    expect(calls).toHaveLength(1);
    const call = calls[0]!;
    expect(call.url).toBe('http://127.0.0.1:8080/messages/unread');
    expect(call.init.method).toBe('GET');
    expect(headerOf(call.init, 'Authorization')).toBe('Bearer test-token');
    expect(headerOf(call.init, 'X-Agent-ID')).toBe('claude-code');
    expect(headerOf(call.init, 'Content-Type')).toBe('application/json');
    expect(call.init.body).toBeUndefined();
  });

  it('serializes query parameters', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({}));
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await client.get('/messages/unread', {
      source: 'discord',
      limit: 25,
      since: '2026-04-25T15:32:11Z',
    });

    const url = new URL(calls[0]!.url);
    expect(url.searchParams.get('source')).toBe('discord');
    expect(url.searchParams.get('limit')).toBe('25');
    expect(url.searchParams.get('since')).toBe('2026-04-25T15:32:11Z');
  });

  it('skips null and undefined query values', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({}));
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await client.get('/messages/unread', {
      source: 'discord',
      channel_id: undefined,
      since: null,
    });

    const url = new URL(calls[0]!.url);
    expect(url.searchParams.get('source')).toBe('discord');
    expect(url.searchParams.has('channel_id')).toBe(false);
    expect(url.searchParams.has('since')).toBe(false);
  });

  it('normalizes a path without a leading slash', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({}));
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await client.get('messages/unread');
    expect(calls[0]!.url).toBe('http://127.0.0.1:8080/messages/unread');
  });

  it('strips a trailing slash on the base URL', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({}));
    const client = createHttpClient({
      config: { ...TEST_CONFIG, baseUrl: 'http://127.0.0.1:8080/' },
      fetchImpl,
    });

    await client.get('/messages/unread');
    expect(calls[0]!.url).toBe('http://127.0.0.1:8080/messages/unread');
  });
});

describe('createHttpClient.post', () => {
  it('sends POST with JSON body and default headers', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({ marked_count: 2 }),
    );
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.post('/messages/mark_read', {
      message_ids: ['msg_01HXYZ', 'msg_01HABC'],
    });

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data).toEqual({ marked_count: 2 });
    }
    const call = calls[0]!;
    expect(call.init.method).toBe('POST');
    expect(call.init.body).toBe(
      JSON.stringify({ message_ids: ['msg_01HXYZ', 'msg_01HABC'] }),
    );
    expect(headerOf(call.init, 'Content-Type')).toBe('application/json');
    expect(headerOf(call.init, 'Authorization')).toBe('Bearer test-token');
    expect(headerOf(call.init, 'X-Agent-ID')).toBe('claude-code');
  });

  it('does NOT add an Idempotency-Key by default', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({}));
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await client.post('/messages/mark_read', { message_ids: [] });
    expect(headerOf(calls[0]!.init, 'Idempotency-Key')).toBeUndefined();
  });

  it('passes through caller-supplied Idempotency-Key in extraHeaders', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({ message_id: 'msg_01HXYZ', sent_at: '2026-04-25T15:32:13Z' }),
    );
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const key = randomIdempotencyKey();
    await client.post(
      '/messages/send',
      { channel_id: '+15551234567', text: 'hi' },
      { 'Idempotency-Key': key },
    );

    expect(headerOf(calls[0]!.init, 'Idempotency-Key')).toBe(key);
  });

  it('extraHeaders can override defaults (last-write-wins)', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({}));
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await client.post('/messages/send', {}, { 'Content-Type': 'application/x-custom' });
    expect(headerOf(calls[0]!.init, 'Content-Type')).toBe('application/x-custom');
  });
});

describe('randomIdempotencyKey', () => {
  it('returns a UUID-shaped string', () => {
    const key = randomIdempotencyKey();
    expect(key).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    );
  });

  it('returns a fresh value on each call', () => {
    const a = randomIdempotencyKey();
    const b = randomIdempotencyKey();
    expect(a).not.toBe(b);
  });
});

describe('non-2xx response handling', () => {
  it('maps a §7.4.12 error envelope to {ok:false, code, message, body}', async () => {
    const errorBody = {
      error: {
        code: 'CHANNEL_NOT_FOUND',
        message: 'Unknown channel',
      },
    };
    const { fetchImpl } = makeFetchStub(
      () =>
        new Response(JSON.stringify(errorBody), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.post('/messages/send', { channel_id: 'x', text: 'hi' });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.status).toBe(404);
      expect(result.code).toBe('CHANNEL_NOT_FOUND');
      expect(result.message).toBe('Unknown channel');
      expect(result.body).toEqual(errorBody);
    }
  });

  it('falls back to INTERNAL_ERROR when the body is not the standard envelope', async () => {
    const { fetchImpl } = makeFetchStub(
      () =>
        new Response('plain text error', {
          status: 500,
          statusText: 'Internal Server Error',
        }),
    );
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.get('/messages/unread');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.status).toBe(500);
      expect(result.code).toBe('INTERNAL_ERROR');
      expect(result.message).toContain('Internal Server Error');
      expect(result.body).toBe('plain text error');
    }
  });

  it('handles a 401 with the standard envelope', async () => {
    const { fetchImpl } = makeFetchStub(
      () =>
        new Response(
          JSON.stringify({ error: { code: 'UNAUTHORIZED', message: 'bad token' } }),
          { status: 401, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.get('/messages/unread');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('UNAUTHORIZED');
      expect(result.status).toBe(401);
    }
  });
});

describe('204 No Content', () => {
  it('returns ok with undefined data for 204', async () => {
    const { fetchImpl } = makeFetchStub(() => new Response(null, { status: 204 }));
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.post('/typing', { channel_id: 'x' });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.status).toBe(204);
      expect(result.data).toBeUndefined();
    }
  });
});

describe('network failures', () => {
  it('returns NETWORK_ERROR when fetch rejects (connection refused)', async () => {
    const fetchImpl = (async () => {
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.get('/messages/unread');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('NETWORK_ERROR');
      expect(result.status).toBe(NETWORK_ERROR_STATUS);
      expect(result.message).toContain('fetch failed');
    }
  });

  it('returns NETWORK_ERROR when fetch rejects with a non-Error value', async () => {
    const fetchImpl = (async () => {
      // Reject with a string just to prove the code path tolerates it.
      throw 'boom';
    }) as unknown as typeof fetch;
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.post('/messages/send', {});
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('NETWORK_ERROR');
      expect(result.message).toBe('boom');
    }
  });

  it('returns NETWORK_ERROR on simulated abort/timeout', async () => {
    const fetchImpl = (async () => {
      const err = new Error('The operation was aborted');
      err.name = 'AbortError';
      throw err;
    }) as unknown as typeof fetch;
    const client = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await client.get('/messages/unread');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('NETWORK_ERROR');
    }
  });
});

describe('default fetch resolution', () => {
  let originalFetch: typeof globalThis.fetch | undefined;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    if (originalFetch) {
      globalThis.fetch = originalFetch;
    }
  });

  it('uses globalThis.fetch when no fetchImpl is provided', async () => {
    const stub = vi.fn(async () => jsonResponse({ ok: 1 }));
    vi.stubGlobal('fetch', stub);

    const client = createHttpClient({ config: TEST_CONFIG });
    await client.get('/healthz');
    expect(stub).toHaveBeenCalledTimes(1);
  });
});
