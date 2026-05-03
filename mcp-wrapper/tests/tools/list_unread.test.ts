// Unit tests for src/tools/list_unread.ts.
//
// The HttpClient is mocked with `vi.fn()` so we can assert the exact
// (path, query) pair the tool sends and stage the response shape returned
// to the agent. We never touch real fetch.

import { describe, expect, it, vi } from 'vitest';

import type { HttpClient, HttpResult } from '../../src/http.js';
import {
  LIST_UNREAD_SOURCES,
  listUnreadInputSchema,
  listUnreadMessages,
} from '../../src/tools/list_unread.js';

interface GetCall {
  path: string;
  query: Record<string, unknown> | undefined;
}

function makeHttp(response: HttpResult<unknown>): {
  http: HttpClient;
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
  calls: GetCall[];
} {
  const calls: GetCall[] = [];
  const get = vi.fn(async (path: string, query?: Record<string, unknown>) => {
    calls.push({ path, query });
    return response;
  });
  const post = vi.fn(async () => {
    throw new Error('post should not be called by list_unread_messages');
  });
  const http: HttpClient = {
    get: get as unknown as HttpClient['get'],
    post: post as unknown as HttpClient['post'],
  };
  return { http, get, post, calls };
}

describe('listUnreadMessages module shape', () => {
  it('exports a self-contained tool descriptor', () => {
    expect(listUnreadMessages.name).toBe('list_unread_messages');
    expect(typeof listUnreadMessages.description).toBe('string');
    expect(listUnreadMessages.description.length).toBeGreaterThan(0);
    // A handler that takes (input, http) -> Promise<ToolResult>.
    expect(typeof listUnreadMessages.handler).toBe('function');
    expect(listUnreadMessages.handler.length).toBe(2);
    // The input schema is a zod schema (has .parse).
    expect(typeof listUnreadMessages.inputSchema.parse).toBe('function');
  });

  it('description mentions the optional filters', () => {
    const desc = listUnreadMessages.description.toLowerCase();
    expect(desc).toContain('source');
    expect(desc).toContain('channel_id');
    expect(desc).toContain('since');
    expect(desc).toContain('limit');
  });
});

describe('listUnreadInputSchema validation', () => {
  it('accepts an empty object (all fields optional)', () => {
    expect(listUnreadInputSchema.parse({})).toEqual({});
  });

  it('accepts a fully populated valid input', () => {
    const parsed = listUnreadInputSchema.parse({
      since: '2026-04-25T15:32:11Z',
      source: 'discord',
      channel_id: 'C123',
      limit: 50,
    });
    expect(parsed.source).toBe('discord');
    expect(parsed.limit).toBe(50);
  });

  it('rejects an unknown source enum value', () => {
    const result = listUnreadInputSchema.safeParse({ source: 'slack' });
    expect(result.success).toBe(false);
  });

  it('accepts both allowlisted source values', () => {
    for (const src of LIST_UNREAD_SOURCES) {
      expect(listUnreadInputSchema.parse({ source: src }).source).toBe(src);
    }
  });

  it('rejects limit below the lower bound', () => {
    expect(listUnreadInputSchema.safeParse({ limit: 0 }).success).toBe(false);
  });

  it('rejects limit above the upper bound', () => {
    expect(listUnreadInputSchema.safeParse({ limit: 101 }).success).toBe(false);
  });

  it('rejects non-integer limit', () => {
    expect(listUnreadInputSchema.safeParse({ limit: 1.5 }).success).toBe(false);
  });

  it('rejects non-string since', () => {
    expect(listUnreadInputSchema.safeParse({ since: 12345 }).success).toBe(false);
  });
});

describe('listUnreadMessages.handler', () => {
  const okResponse = {
    messages: [
      { id: 'msg_01HXYZ', source: 'discord', channel_id: 'C1', text: 'hi' },
    ],
    next_since: '2026-04-25T15:35:00Z',
  };

  function ok(data: unknown): HttpResult<unknown> {
    return { ok: true, status: 200, data };
  }

  it('calls GET /messages/unread with the supplied filters', async () => {
    const { http, calls } = makeHttp(ok(okResponse));

    await listUnreadMessages.handler(
      {
        source: 'discord',
        channel_id: 'C1',
        since: '2026-04-25T15:32:11Z',
        limit: 25,
      },
      http,
    );

    expect(calls).toHaveLength(1);
    expect(calls[0]!.path).toBe('/messages/unread');
    expect(calls[0]!.query).toEqual({
      source: 'discord',
      channel_id: 'C1',
      since: '2026-04-25T15:32:11Z',
      limit: 25,
    });
  });

  it('omits undefined fields from the query (no spurious params)', async () => {
    const { http, calls } = makeHttp(ok(okResponse));

    await listUnreadMessages.handler({ source: 'imessage' }, http);

    expect(calls[0]!.query).toEqual({ source: 'imessage' });
    const query = calls[0]!.query as Record<string, unknown>;
    expect(Object.keys(query)).toEqual(['source']);
  });

  it('omits all fields when input is empty', async () => {
    const { http, calls } = makeHttp(ok(okResponse));

    await listUnreadMessages.handler({}, http);

    expect(calls[0]!.path).toBe('/messages/unread');
    expect(calls[0]!.query).toEqual({});
  });

  it('returns content as a single text block with pretty-printed JSON', async () => {
    const { http } = makeHttp(ok(okResponse));

    const result = await listUnreadMessages.handler({}, http);

    expect(result.content).toHaveLength(1);
    expect(result.content[0]!.type).toBe('text');
    const parsed = JSON.parse(result.content[0]!.text) as typeof okResponse;
    expect(parsed.messages).toEqual(okResponse.messages);
    expect(parsed.next_since).toBe(okResponse.next_since);
    // Pretty-printed (indent of 2) is a stable contract for human readers.
    expect(result.content[0]!.text).toContain('\n  ');
  });

  it('exposes structured data alongside the text content', async () => {
    const { http } = makeHttp(ok(okResponse));

    const result = await listUnreadMessages.handler({}, http);

    expect(result.data).toBeDefined();
    expect(result.data!.messages).toEqual(okResponse.messages);
    expect(result.data!.next_since).toBe(okResponse.next_since);
    expect(result.isError).toBeFalsy();
  });

  it('normalizes a missing next_since to null', async () => {
    const { http } = makeHttp(ok({ messages: [] }));

    const result = await listUnreadMessages.handler({}, http);

    expect(result.data!.messages).toEqual([]);
    expect(result.data!.next_since).toBeNull();
  });

  it('rejects an invalid source at the handler boundary', async () => {
    const { http, get } = makeHttp(ok(okResponse));

    await expect(
      // Cast to bypass TS so we can prove runtime validation kicks in.
      listUnreadMessages.handler(
        { source: 'slack' as unknown as 'discord' },
        http,
      ),
    ).rejects.toThrow();

    expect(get).not.toHaveBeenCalled();
  });

  it('rejects an out-of-range limit at the handler boundary', async () => {
    const { http, get } = makeHttp(ok(okResponse));

    await expect(
      listUnreadMessages.handler({ limit: 9999 }, http),
    ).rejects.toThrow();

    expect(get).not.toHaveBeenCalled();
  });

  it('returns an error tool result when the adapter responds non-2xx', async () => {
    const errResponse: HttpResult<unknown> = {
      ok: false,
      status: 401,
      code: 'UNAUTHORIZED',
      message: 'bad token',
      body: { error: { code: 'UNAUTHORIZED', message: 'bad token' } },
    };
    const { http } = makeHttp(errResponse);

    const result = await listUnreadMessages.handler({}, http);

    expect(result.isError).toBe(true);
    const parsed = JSON.parse(result.content[0]!.text) as {
      error: { code: string; message: string; status: number };
    };
    expect(parsed.error.code).toBe('UNAUTHORIZED');
    expect(parsed.error.message).toBe('bad token');
    expect(parsed.error.status).toBe(401);
    expect(result.data).toBeUndefined();
  });

  it('returns an error tool result on transport (NETWORK_ERROR) failures', async () => {
    const errResponse: HttpResult<unknown> = {
      ok: false,
      status: 0,
      code: 'NETWORK_ERROR',
      message: 'fetch failed',
    };
    const { http } = makeHttp(errResponse);

    const result = await listUnreadMessages.handler({}, http);

    expect(result.isError).toBe(true);
    const parsed = JSON.parse(result.content[0]!.text) as {
      error: { code: string; status: number };
    };
    expect(parsed.error.code).toBe('NETWORK_ERROR');
    expect(parsed.error.status).toBe(0);
  });
});
