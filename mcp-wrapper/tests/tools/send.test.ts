// Unit tests for src/tools/send.ts.
//
// We construct an `HttpClient` against a stubbed fetch (mirroring the pattern
// in tests/http.test.ts) and assert:
//   * Tool metadata (name, description, schema shape).
//   * Input validation: required fields, mutual exclusion of url/path on
//     attachments, optional fields propagate.
//   * The handler hits `POST /messages/send` with a fresh `Idempotency-Key`
//     header (UUID v4) on each call.
//   * The handler returns the `{ message_id, sent_at }` payload from the
//     adapter on 2xx, and a structured `HttpErr` on non-2xx.

/* eslint-disable no-undef */

import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WrapperConfig } from '../../src/config.js';
import { createHttpClient } from '../../src/http.js';
import { sendMessage } from '../../src/tools/send.js';

interface RecordedCall {
  url: string;
  init: RequestInit;
}

const TEST_CONFIG: WrapperConfig = {
  baseUrl: 'http://127.0.0.1:8080',
  bearerToken: 'test-token',
  agentId: 'claude-code',
};

const UUID_V4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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

describe('sendMessage tool metadata', () => {
  it('declares the canonical tool name', () => {
    expect(sendMessage.name).toBe('send_message');
  });

  it('exposes a non-empty description', () => {
    expect(typeof sendMessage.description).toBe('string');
    expect(sendMessage.description.length).toBeGreaterThan(0);
  });

  it('input schema accepts the minimal valid payload', () => {
    const parsed = sendMessage.inputSchema.parse({
      channel_id: '+15551234567',
      text: 'hi',
    });
    expect(parsed.channel_id).toBe('+15551234567');
    expect(parsed.text).toBe('hi');
    expect(parsed.reply_to).toBeUndefined();
    expect(parsed.attachments).toBeUndefined();
  });

  it('input schema accepts reply_to and attachments', () => {
    const parsed = sendMessage.inputSchema.parse({
      channel_id: 'C1',
      text: 'reply',
      reply_to: 'msg_01HXYZ',
      attachments: [{ url: 'https://example.com/a.png' }, { path: '/tmp/b.png' }],
    });
    expect(parsed.reply_to).toBe('msg_01HXYZ');
    expect(parsed.attachments).toHaveLength(2);
  });

  it('input schema rejects missing required fields', () => {
    expect(() => sendMessage.inputSchema.parse({ text: 'hi' })).toThrow();
    expect(() => sendMessage.inputSchema.parse({ channel_id: 'C1' })).toThrow();
  });

  it('input schema rejects an attachment with both url and path', () => {
    expect(() =>
      sendMessage.inputSchema.parse({
        channel_id: 'C1',
        text: 'hi',
        attachments: [{ url: 'https://x', path: '/tmp/x' }],
      }),
    ).toThrow();
  });

  it('input schema rejects an attachment with neither url nor path', () => {
    expect(() =>
      sendMessage.inputSchema.parse({
        channel_id: 'C1',
        text: 'hi',
        attachments: [{}],
      }),
    ).toThrow();
  });
});

describe('sendMessage.handler', () => {
  it('POSTs to /messages/send with the request body', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({
        message_id: 'msg_01HXYZ123456789ABCDEFGHJKM',
        sent_at: '2026-04-25T15:32:13Z',
      }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await sendMessage.handler(
      { channel_id: '+15551234567', text: 'hello' },
      http,
    );

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data).toEqual({
        message_id: 'msg_01HXYZ123456789ABCDEFGHJKM',
        sent_at: '2026-04-25T15:32:13Z',
      });
    }

    expect(calls).toHaveLength(1);
    const call = calls[0]!;
    expect(call.url).toBe('http://127.0.0.1:8080/messages/send');
    expect(call.init.method).toBe('POST');
    expect(call.init.body).toBe(
      JSON.stringify({ channel_id: '+15551234567', text: 'hello' }),
    );
  });

  it('attaches an Idempotency-Key (UUID v4 shape) on each call', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({
        message_id: 'msg_01HXYZ123456789ABCDEFGHJKM',
        sent_at: '2026-04-25T15:32:13Z',
      }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await sendMessage.handler({ channel_id: 'C1', text: 'a' }, http);
    await sendMessage.handler({ channel_id: 'C1', text: 'b' }, http);

    const key1 = headerOf(calls[0]!.init, 'Idempotency-Key');
    const key2 = headerOf(calls[1]!.init, 'Idempotency-Key');

    expect(key1).toMatch(UUID_V4_PATTERN);
    expect(key2).toMatch(UUID_V4_PATTERN);
    expect(key1).not.toBe(key2);
  });

  it('forwards reply_to and attachments in the body', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({
        message_id: 'msg_01HXYZ123456789ABCDEFGHJKM',
        sent_at: '2026-04-25T15:32:13Z',
      }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await sendMessage.handler(
      {
        channel_id: 'C1',
        text: 'see attached',
        reply_to: 'msg_01HABC123456789ABCDEFGHJKM',
        attachments: [
          { url: 'https://example.com/a.png' },
          { path: '/tmp/b.png' },
        ],
      },
      http,
    );

    const body = JSON.parse(calls[0]!.init.body as string) as Record<string, unknown>;
    expect(body.channel_id).toBe('C1');
    expect(body.text).toBe('see attached');
    expect(body.reply_to).toBe('msg_01HABC123456789ABCDEFGHJKM');
    expect(body.attachments).toEqual([
      { url: 'https://example.com/a.png' },
      { path: '/tmp/b.png' },
    ]);
  });

  it('sends default Authorization and X-Agent-ID headers', async () => {
    const { fetchImpl, calls } = makeFetchStub(() =>
      jsonResponse({
        message_id: 'msg_01HXYZ123456789ABCDEFGHJKM',
        sent_at: '2026-04-25T15:32:13Z',
      }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    await sendMessage.handler({ channel_id: 'C1', text: 'hi' }, http);

    expect(headerOf(calls[0]!.init, 'Authorization')).toBe('Bearer test-token');
    expect(headerOf(calls[0]!.init, 'X-Agent-ID')).toBe('claude-code');
    expect(headerOf(calls[0]!.init, 'Content-Type')).toBe('application/json');
  });

  it('returns the structured HttpErr on a non-2xx response', async () => {
    const errorBody = {
      error: { code: 'CHANNEL_NOT_FOUND', message: 'Unknown channel' },
    };
    const { fetchImpl } = makeFetchStub(
      () =>
        new Response(JSON.stringify(errorBody), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await sendMessage.handler(
      { channel_id: 'C1', text: 'hi' },
      http,
    );

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.status).toBe(404);
      expect(result.code).toBe('CHANNEL_NOT_FOUND');
      expect(result.message).toBe('Unknown channel');
      expect(result.body).toEqual(errorBody);
    }
  });

  it('returns NETWORK_ERROR when fetch rejects', async () => {
    const fetchImpl = (async () => {
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    const result = await sendMessage.handler(
      { channel_id: 'C1', text: 'hi' },
      http,
    );

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('NETWORK_ERROR');
    }
  });

  it('rejects invalid input via zod before issuing any HTTP call', async () => {
    const { fetchImpl, calls } = makeFetchStub(() => jsonResponse({}));
    const http = createHttpClient({ config: TEST_CONFIG, fetchImpl });

    // `text` missing — schema parse must throw before fetch is called.
    await expect(
      sendMessage.handler(
        { channel_id: 'C1' } as unknown as Parameters<typeof sendMessage.handler>[0],
        http,
      ),
    ).rejects.toThrow();
    expect(calls).toHaveLength(0);
  });
});
