// Unit tests for src/tools/context.ts.
//
// We exercise the input schema (defaults, caps, regex) and the handler's HTTP
// call shape. The HTTP client is stubbed via createHttpClient with a mock
// fetch — same pattern as tests/http.test.ts — so no real network calls.
//
// `Response` is a Node 20+ global. The flat eslint config doesn't list it
// under globals, so silence no-undef like the http.test.ts file does.

/* eslint-disable no-undef */

import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WrapperConfig } from '../../src/config.js';
import { createHttpClient } from '../../src/http.js';
import {
  MESSAGE_ID,
  getMessageContext,
  getMessageContextInput,
} from '../../src/tools/context.js';

interface RecordedCall {
  url: string;
  init: RequestInit;
}

const TEST_CONFIG: WrapperConfig = {
  baseUrl: 'http://127.0.0.1:8080',
  bearerToken: 'test-token',
  agentId: 'claude-code',
};

const VALID_MSG_ID = 'msg_01HXYZABCDEFGHJKMNPQRSTVWX';

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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('getMessageContext tool descriptor', () => {
  it('exports the spec-defined name and a non-empty description', () => {
    expect(getMessageContext.name).toBe('get_message_context');
    expect(typeof getMessageContext.description).toBe('string');
    expect(getMessageContext.description.length).toBeGreaterThan(0);
  });

  it('exposes the zod input schema', () => {
    expect(getMessageContext.inputSchema).toBe(getMessageContextInput);
  });

  it('exposes a handler function', () => {
    expect(typeof getMessageContext.handler).toBe('function');
  });
});

describe('getMessageContextInput schema', () => {
  it('accepts a minimal valid input and applies before/after defaults of 5', () => {
    const parsed = getMessageContextInput.parse({
      channel_id: '+15551234567',
      around_message_id: VALID_MSG_ID,
    });
    expect(parsed.channel_id).toBe('+15551234567');
    expect(parsed.around_message_id).toBe(VALID_MSG_ID);
    expect(parsed.before).toBe(5);
    expect(parsed.after).toBe(5);
  });

  it('accepts explicit before/after values within range', () => {
    const parsed = getMessageContextInput.parse({
      channel_id: 'C12345',
      around_message_id: VALID_MSG_ID,
      before: 0,
      after: 50,
    });
    expect(parsed.before).toBe(0);
    expect(parsed.after).toBe(50);
  });

  it('rejects before > 50', () => {
    const r = getMessageContextInput.safeParse({
      channel_id: 'C',
      around_message_id: VALID_MSG_ID,
      before: 51,
    });
    expect(r.success).toBe(false);
  });

  it('rejects after > 50', () => {
    const r = getMessageContextInput.safeParse({
      channel_id: 'C',
      around_message_id: VALID_MSG_ID,
      after: 51,
    });
    expect(r.success).toBe(false);
  });

  it('rejects negative before', () => {
    const r = getMessageContextInput.safeParse({
      channel_id: 'C',
      around_message_id: VALID_MSG_ID,
      before: -1,
    });
    expect(r.success).toBe(false);
  });

  it('rejects non-integer before/after', () => {
    const r = getMessageContextInput.safeParse({
      channel_id: 'C',
      around_message_id: VALID_MSG_ID,
      before: 1.5,
    });
    expect(r.success).toBe(false);
  });

  it('rejects an around_message_id without the msg_ prefix', () => {
    const r = getMessageContextInput.safeParse({
      channel_id: 'C',
      around_message_id: '01HXYZABCDEFGHJKMNPQRSTVWX',
    });
    expect(r.success).toBe(false);
  });

  it('rejects an around_message_id with forbidden ULID chars (I, L, O, U)', () => {
    for (const bad of [
      'msg_I1HXYZABCDEFGHJKMNPQRSTVWX',
      'msg_L1HXYZABCDEFGHJKMNPQRSTVWX',
      'msg_O1HXYZABCDEFGHJKMNPQRSTVWX',
      'msg_U1HXYZABCDEFGHJKMNPQRSTVWX',
    ]) {
      const r = getMessageContextInput.safeParse({
        channel_id: 'C',
        around_message_id: bad,
      });
      expect(r.success, `expected ${bad} to be rejected`).toBe(false);
    }
  });

  it('rejects an around_message_id of the wrong length', () => {
    const r = getMessageContextInput.safeParse({
      channel_id: 'C',
      around_message_id: 'msg_01HXYZ',
    });
    expect(r.success).toBe(false);
  });

  it('rejects a missing channel_id', () => {
    const r = getMessageContextInput.safeParse({
      around_message_id: VALID_MSG_ID,
    });
    expect(r.success).toBe(false);
  });

  it('MESSAGE_ID regex matches valid ids and rejects invalid ones', () => {
    expect(MESSAGE_ID.test(VALID_MSG_ID)).toBe(true);
    expect(MESSAGE_ID.test('msg_' + 'A'.repeat(26))).toBe(true);
    expect(MESSAGE_ID.test('msg_' + 'a'.repeat(26))).toBe(false);
    expect(MESSAGE_ID.test('msg_')).toBe(false);
  });
});

describe('getMessageContext.handler', () => {
  it('issues GET /messages/context with all four query params', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({ messages: [] }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await getMessageContext.handler(
      {
        channel_id: '+15551234567',
        around_message_id: VALID_MSG_ID,
        before: 3,
        after: 7,
      },
      http,
    );

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.status).toBe(200);
      expect(result.data).toEqual({ messages: [] });
    }
    expect(calls).toHaveLength(1);
    const call = calls[0]!;
    expect(call.init.method).toBe('GET');
    const url = new URL(call.url);
    expect(url.pathname).toBe('/messages/context');
    expect(url.searchParams.get('channel_id')).toBe('+15551234567');
    expect(url.searchParams.get('around_message_id')).toBe(VALID_MSG_ID);
    expect(url.searchParams.get('before')).toBe('3');
    expect(url.searchParams.get('after')).toBe('7');
  });

  it('applies defaults when before/after are omitted', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({ messages: [] }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await getMessageContext.handler(
      {
        channel_id: 'C12345',
        around_message_id: VALID_MSG_ID,
      } as unknown as Parameters<typeof getMessageContext.handler>[0],
      http,
    );

    const url = new URL(calls[0]!.url);
    expect(url.searchParams.get('before')).toBe('5');
    expect(url.searchParams.get('after')).toBe('5');
  });

  it('returns the parsed envelope list as-is', async () => {
    const fakeMessages = [
      { id: 'msg_01OLDEST00000000000000000', text: 'older' },
      { id: VALID_MSG_ID, text: 'target' },
      { id: 'msg_01NEWEST00000000000000000', text: 'newer' },
    ];
    const { fetchImpl } = makeFetchStub(() =>
      jsonResponse({ messages: fakeMessages }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await getMessageContext.handler(
      {
        channel_id: 'C12345',
        around_message_id: VALID_MSG_ID,
        before: 5,
        after: 5,
      },
      http,
    );

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.messages).toEqual(fakeMessages);
    }
  });

  it('propagates a 404 MESSAGE_NOT_FOUND response without throwing', async () => {
    const { fetchImpl } = makeFetchStub(
      () =>
        new Response(
          JSON.stringify({
            error: { code: 'MESSAGE_NOT_FOUND', message: 'not found' },
          }),
          {
            status: 404,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await getMessageContext.handler(
      {
        channel_id: 'C12345',
        around_message_id: VALID_MSG_ID,
        before: 5,
        after: 5,
      },
      http,
    );

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.status).toBe(404);
      expect(result.code).toBe('MESSAGE_NOT_FOUND');
    }
  });

  it('throws a ZodError when input fails validation (e.g. bad message id)', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({ messages: [] }));
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await expect(
      getMessageContext.handler(
        {
          channel_id: 'C',
          around_message_id: 'not-a-msg-id',
          before: 5,
          after: 5,
        } as unknown as Parameters<typeof getMessageContext.handler>[0],
        http,
      ),
    ).rejects.toThrow();
    expect(calls).toHaveLength(0);
  });

  it('throws when before exceeds the cap of 50', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({ messages: [] }));
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await expect(
      getMessageContext.handler(
        {
          channel_id: 'C',
          around_message_id: VALID_MSG_ID,
          before: 51,
          after: 5,
        },
        http,
      ),
    ).rejects.toThrow();
    expect(calls).toHaveLength(0);
  });

  it('forwards the configured Authorization and X-Agent-ID headers', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({ messages: [] }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await getMessageContext.handler(
      {
        channel_id: 'C',
        around_message_id: VALID_MSG_ID,
        before: 5,
        after: 5,
      },
      http,
    );

    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer test-token');
    expect(headers['X-Agent-ID']).toBe('claude-code');
  });
});
