// End-to-end harness for the four MCP tools, exercised through a real
// MCP-SDK Client.
//
// Spec acceptance criterion: "all 4 tools verified by automated MCP-SDK
// client harness" (specs/agent-messaging-channel-SPEC.md §10.1).
//
// Approach choice — in-process MCP server + mocked adapter HTTP:
//   The task description offered two options:
//     (a) Boot the Python adapter as a subprocess + spawn `node ./dist/index.js`
//         over stdio.
//     (b) Mock the adapter HTTP in-process so the test never depends on Python.
//   We picked (b). Reasons:
//     1. dist/index.js (today, pre-#65 wiring) registers ZERO tools — option
//        (a) would assert against a stub server. Option (b) lets us register
//        the real tool modules from src/tools/* against a fresh MCP server,
//        exercising the same handler code that ships in dist.
//     2. Spinning up the Python adapter from a TypeScript test would couple
//        the wrapper test suite to `uv`, an ephemeral port allocator, and a
//        teardown ritual — none of which exist today.
//     3. The user's task brief explicitly authorised the mock approach with
//        the same acceptance criterion.
//
// What this harness covers (the FULL wrapper code path):
//   * MCP `initialize` handshake (clientInfo / serverInfo / protocolVersion)
//   * `tools/list` — confirms all four tools are advertised
//   * `tools/call` for each tool with realistic inputs
//   * Tool handler dispatch (zod input parsing → http call)
//   * HTTP layer (URL building, query params, default + per-call headers)
//   * Response transformation back to an MCP `content[]` payload
//
// What this harness intentionally does NOT cover:
//   * The Python adapter. (Adapter contract tests live under tests/api/.)
//   * stdio framing. (The smoke test in tests/smoke.test.ts already spawns
//     dist/index.js and asserts the initialize handshake over stdio.)
//
// Cross-runtime: relies only on Node 20+ globals (fetch, URL, Response).

/* eslint-disable no-undef */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WrapperConfig } from '../src/config.js';
import { createHttpClient, type HttpClient } from '../src/http.js';
import { listUnreadMessages } from '../src/tools/list_unread.js';
import { sendMessage } from '../src/tools/send.js';
import { markRead } from '../src/tools/mark_read.js';
import { getMessageContext } from '../src/tools/context.js';
import { mapHttpErrorToMcpResponse } from '../src/errors.js';
import { SERVER_NAME, SERVER_VERSION } from '../src/version.js';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const TEST_CONFIG: WrapperConfig = {
  baseUrl: 'http://127.0.0.1:8080',
  bearerToken: 'test-bearer-token',
  agentId: 'test-agent',
};

// A canonical Discord-shaped envelope (spec blueprint §3).
const DISCORD_ENVELOPE = {
  id: 'msg_01HXYZABCDEFGHJKMNPQRSTVWX',
  source: 'discord' as const,
  channel_id: 'discord:1234567890',
  channel_type: 'dm' as const,
  sender: {
    id: 'discord:user:9999',
    display_name: 'Alice',
    person_id: 'alice',
  },
  text: 'hey from discord',
  attachments: [],
  reply_to: null,
  timestamp: '2026-04-25T15:32:11Z',
  direction: 'inbound' as const,
  raw: { snowflake: '1234567890' },
};

// A canonical iMessage-shaped envelope (E.164 channel, no person_id linkage).
const IMESSAGE_ENVELOPE = {
  id: 'msg_01HABCDEFGHJKMNPQRSTVWXYZ0',
  source: 'imessage' as const,
  channel_id: '+15551234567',
  channel_type: 'dm' as const,
  sender: {
    id: '+15551234567',
    display_name: 'Bob',
    person_id: null,
  },
  text: 'hey from iMessage',
  attachments: [
    {
      id: 'att_01HABCDEFGHJKMNPQRSTVWXYZ0',
      url: 'http://127.0.0.1:8080/attachments/att_01HABCDEFGHJKMNPQRSTVWXYZ0',
      mime: 'image/png',
      size_bytes: 1024,
    },
  ],
  reply_to: null,
  timestamp: '2026-04-25T15:33:00Z',
  direction: 'inbound' as const,
  raw: { rowid: 42 },
};

// ---------------------------------------------------------------------------
// Mock adapter — records every HTTP call and serves canned responses.
// ---------------------------------------------------------------------------

interface RecordedCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | undefined;
}

interface MockAdapter {
  fetchImpl: typeof fetch;
  calls: RecordedCall[];
  /** Set the next response for any path. */
  enqueue(path: string, response: Response | (() => Response)): void;
}

function createMockAdapter(): MockAdapter {
  const calls: RecordedCall[] = [];
  const queues = new Map<string, Array<Response | (() => Response)>>();

  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const urlStr = typeof input === 'string' ? input : input.toString();
    const url = new URL(urlStr);
    const method = init?.method ?? 'GET';
    const headers = init?.headers as Record<string, string> | undefined;
    const body = typeof init?.body === 'string' ? init.body : undefined;

    calls.push({
      url: urlStr,
      method,
      headers: { ...(headers ?? {}) },
      body,
    });

    const queue = queues.get(url.pathname);
    if (!queue || queue.length === 0) {
      return new Response(
        JSON.stringify({
          error: {
            code: 'INTERNAL_ERROR',
            message: `mock adapter: no canned response for ${method} ${url.pathname}`,
          },
        }),
        { status: 500, headers: { 'Content-Type': 'application/json' } },
      );
    }
    const next = queue.shift()!;
    return typeof next === 'function' ? next() : next;
  }) as unknown as typeof fetch;

  return {
    fetchImpl,
    calls,
    enqueue(path: string, response: Response | (() => Response)) {
      const queue = queues.get(path) ?? [];
      queue.push(response);
      queues.set(path, queue);
    },
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function getHeader(call: RecordedCall, name: string): string | undefined {
  for (const [k, v] of Object.entries(call.headers)) {
    if (k.toLowerCase() === name.toLowerCase()) return v;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Build the MCP server with the four real tools registered.
//
// We mirror the wiring that #65 will commit into src/index.ts: each tool
// module exports { name, description, inputSchema, handler }; we register
// them on an McpServer using `registerTool`, with a thin adapter that:
//   * Forwards parsed args to the tool's handler.
//   * Translates HttpResult-shaped errors into the §7.4.12 MCP error payload
//     via `mapHttpErrorToMcpResponse`.
//   * Wraps successful responses in `{ content: [{ type: 'text', text: ... }] }`.
//
// Keeping the wiring inside the test (rather than importing from src/index.ts)
// is deliberate: the test stays runnable today, before #65 lands. When #65
// publishes a `registerAllTools(server, http)` helper, this wiring can be
// replaced by a one-liner without changing any assertions.
// ---------------------------------------------------------------------------

function buildServerWithTools(http: HttpClient): McpServer {
  const server = new McpServer({
    name: SERVER_NAME,
    version: SERVER_VERSION,
  });

  // list_unread_messages — its handler already returns an MCP-shaped result.
  server.registerTool(
    listUnreadMessages.name,
    {
      description: listUnreadMessages.description,
      inputSchema: listUnreadMessages.inputSchema.shape,
    },
    async (args) => {
      const result = await listUnreadMessages.handler(args, http);
      // Re-cast `content` from readonly to the SDK's mutable expected shape.
      return {
        content: result.content.map((c) => ({ ...c })),
        ...(result.isError ? { isError: true as const } : {}),
      };
    },
  );

  // send_message — handler returns raw HttpResult; we map errors here.
  server.registerTool(
    sendMessage.name,
    {
      description: sendMessage.description,
      inputSchema: sendMessage.inputSchema.shape,
    },
    async (args) => {
      const result = await sendMessage.handler(args, http);
      if (!result.ok) {
        const err = mapHttpErrorToMcpResponse(result, {
          resolveBaseUrl: () => TEST_CONFIG.baseUrl,
        });
        return {
          content: err.content.map((c) => ({ ...c })),
          isError: true as const,
        };
      }
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(result.data, null, 2) }],
      };
    },
  );

  // mark_read — same HttpResult-style handler.
  server.registerTool(
    markRead.name,
    {
      description: markRead.description,
      inputSchema: markRead.inputSchema.shape,
    },
    async (args) => {
      const result = await markRead.handler(args, http);
      if (!result.ok) {
        const err = mapHttpErrorToMcpResponse(result, {
          resolveBaseUrl: () => TEST_CONFIG.baseUrl,
        });
        return {
          content: err.content.map((c) => ({ ...c })),
          isError: true as const,
        };
      }
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(result.data, null, 2) }],
      };
    },
  );

  // get_message_context — same HttpResult-style handler.
  server.registerTool(
    getMessageContext.name,
    {
      description: getMessageContext.description,
      inputSchema: getMessageContext.inputSchema.shape,
    },
    async (args) => {
      const result = await getMessageContext.handler(args, http);
      if (!result.ok) {
        const err = mapHttpErrorToMcpResponse(result, {
          resolveBaseUrl: () => TEST_CONFIG.baseUrl,
        });
        return {
          content: err.content.map((c) => ({ ...c })),
          isError: true as const,
        };
      }
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(result.data, null, 2) }],
      };
    },
  );

  return server;
}

// ---------------------------------------------------------------------------
// Harness: wire client + server through an in-memory transport pair.
// ---------------------------------------------------------------------------

interface Harness {
  client: Client;
  server: McpServer;
  adapter: MockAdapter;
  close(): Promise<void>;
}

async function startHarness(): Promise<Harness> {
  const adapter = createMockAdapter();
  const http = createHttpClient({
    config: TEST_CONFIG,
    fetchImpl: adapter.fetchImpl,
  });

  const server = buildServerWithTools(http);
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);

  const client = new Client({
    name: 'amc-mcp-e2e-client',
    version: '0.0.0',
  });
  await client.connect(clientTransport);

  return {
    client,
    server,
    adapter,
    async close() {
      await client.close();
      await server.close();
    },
  };
}

// Helper: pull the JSON payload out of the first text block in a tool result.
function parseFirstTextBlock(result: { content: Array<{ type: string; text?: string }> }): unknown {
  const block = result.content[0];
  if (!block || block.type !== 'text' || typeof block.text !== 'string') {
    throw new Error('expected first content block to be a text block');
  }
  return JSON.parse(block.text);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('MCP wrapper end-to-end (programmatic SDK client)', () => {
  let harness: Harness;

  beforeEach(async () => {
    harness = await startHarness();
  });

  afterEach(async () => {
    await harness.close();
    vi.unstubAllGlobals();
  });

  describe('initialize handshake', () => {
    it('reports the wrapper server identity (name + version)', () => {
      const info = harness.client.getServerVersion();
      expect(info?.name).toBe(SERVER_NAME);
      expect(info?.version).toBe(SERVER_VERSION);
    });

    it('advertises the tools capability after registering tools', () => {
      const caps = harness.client.getServerCapabilities();
      expect(caps?.tools).toBeDefined();
    });
  });

  describe('tools/list advertises all four tools', () => {
    it('returns exactly the four AMC tools (blueprint §6.1)', async () => {
      const list = await harness.client.listTools();
      const names = list.tools.map((t) => t.name).sort();
      expect(names).toEqual(
        ['get_message_context', 'list_unread_messages', 'mark_read', 'send_message'].sort(),
      );

      // Each tool advertises a non-empty description and an input schema.
      for (const tool of list.tools) {
        expect(typeof tool.description).toBe('string');
        expect(tool.description!.length).toBeGreaterThan(0);
        expect(tool.inputSchema).toBeDefined();
      }
    });
  });

  describe('list_unread_messages — Discord envelope', () => {
    it('forwards filters to GET /messages/unread and returns the envelope payload', async () => {
      harness.adapter.enqueue(
        '/messages/unread',
        jsonResponse({ messages: [DISCORD_ENVELOPE], next_since: '2026-04-25T15:33:00Z' }),
      );

      const result = await harness.client.callTool({
        name: 'list_unread_messages',
        arguments: { source: 'discord', limit: 25 },
      });

      // Adapter received the request with the right verb, path, query, headers.
      const call = harness.adapter.calls.at(-1)!;
      expect(call.method).toBe('GET');
      const url = new URL(call.url);
      expect(url.pathname).toBe('/messages/unread');
      expect(url.searchParams.get('source')).toBe('discord');
      expect(url.searchParams.get('limit')).toBe('25');
      expect(getHeader(call, 'Authorization')).toBe(`Bearer ${TEST_CONFIG.bearerToken}`);
      expect(getHeader(call, 'X-Agent-ID')).toBe(TEST_CONFIG.agentId);

      // Response shape made it back through the MCP transport intact.
      const payload = parseFirstTextBlock(result as { content: Array<{ type: string; text?: string }> }) as {
        messages: Array<{ id: string; source: string }>;
        next_since: string;
      };
      expect(payload.messages).toHaveLength(1);
      expect(payload.messages[0]!.id).toBe(DISCORD_ENVELOPE.id);
      expect(payload.messages[0]!.source).toBe('discord');
      expect(payload.next_since).toBe('2026-04-25T15:33:00Z');
    });
  });

  describe('list_unread_messages — iMessage envelope', () => {
    it('relays an iMessage-shaped envelope with phone-number channel id and attachments', async () => {
      harness.adapter.enqueue(
        '/messages/unread',
        jsonResponse({ messages: [IMESSAGE_ENVELOPE], next_since: null }),
      );

      const result = await harness.client.callTool({
        name: 'list_unread_messages',
        arguments: { source: 'imessage' },
      });

      const call = harness.adapter.calls.at(-1)!;
      expect(new URL(call.url).searchParams.get('source')).toBe('imessage');

      const payload = parseFirstTextBlock(result as { content: Array<{ type: string; text?: string }> }) as {
        messages: Array<typeof IMESSAGE_ENVELOPE>;
        next_since: string | null;
      };
      expect(payload.messages[0]!.channel_id).toBe('+15551234567');
      expect(payload.messages[0]!.source).toBe('imessage');
      expect(payload.messages[0]!.attachments).toHaveLength(1);
      expect(payload.messages[0]!.attachments[0]!.id).toMatch(/^att_/);
      expect(payload.next_since).toBeNull();
    });
  });

  describe('send_message', () => {
    it('POSTs to /messages/send with an Idempotency-Key and returns {message_id, sent_at}', async () => {
      harness.adapter.enqueue(
        '/messages/send',
        jsonResponse({
          message_id: 'msg_01HZZZABCDEFGHJKMNPQRSTVWX',
          sent_at: '2026-04-25T15:34:00Z',
        }),
      );

      const result = await harness.client.callTool({
        name: 'send_message',
        arguments: {
          channel_id: 'discord:1234567890',
          text: 'hello back',
        },
      });

      const call = harness.adapter.calls.at(-1)!;
      expect(call.method).toBe('POST');
      expect(new URL(call.url).pathname).toBe('/messages/send');
      expect(JSON.parse(call.body!)).toEqual({
        channel_id: 'discord:1234567890',
        text: 'hello back',
      });
      // Idempotency-Key is a UUID v4 generated by the wrapper per call.
      const idem = getHeader(call, 'Idempotency-Key');
      expect(idem).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
      );

      const payload = parseFirstTextBlock(result as { content: Array<{ type: string; text?: string }> }) as {
        message_id: string;
        sent_at: string;
      };
      expect(payload.message_id).toBe('msg_01HZZZABCDEFGHJKMNPQRSTVWX');
      expect(payload.sent_at).toBe('2026-04-25T15:34:00Z');
    });

    it('to an iMessage channel (E.164 number) hits the same endpoint', async () => {
      harness.adapter.enqueue(
        '/messages/send',
        jsonResponse({
          message_id: 'msg_01HZZZABCDEFGHJKMNPQRSTVWY',
          sent_at: '2026-04-25T15:35:00Z',
        }),
      );

      await harness.client.callTool({
        name: 'send_message',
        arguments: {
          channel_id: '+15551234567',
          text: 'reply via iMessage',
        },
      });

      const call = harness.adapter.calls.at(-1)!;
      expect(JSON.parse(call.body!)).toEqual({
        channel_id: '+15551234567',
        text: 'reply via iMessage',
      });
      // Headers stay constant regardless of platform.
      expect(getHeader(call, 'Authorization')).toBe(`Bearer ${TEST_CONFIG.bearerToken}`);
      expect(getHeader(call, 'X-Agent-ID')).toBe(TEST_CONFIG.agentId);
    });

    it('surfaces a §7.4.12 adapter error as an isError MCP response', async () => {
      harness.adapter.enqueue(
        '/messages/send',
        jsonResponse(
          { error: { code: 'CHANNEL_NOT_FOUND', message: 'Unknown channel' } },
          404,
        ),
      );

      const result = (await harness.client.callTool({
        name: 'send_message',
        arguments: {
          channel_id: 'discord:does-not-exist',
          text: 'hi',
        },
      })) as { content: Array<{ type: string; text: string }>; isError?: boolean };

      expect(result.isError).toBe(true);
      const text = result.content[0]!.text;
      // The mapped human-friendly message from src/errors.ts:
      expect(text).toContain('Channel not registered');
    });
  });

  describe('mark_read', () => {
    it('POSTs the message_ids list and returns marked_count', async () => {
      harness.adapter.enqueue(
        '/messages/mark_read',
        jsonResponse({ marked_count: 2 }),
      );

      const result = await harness.client.callTool({
        name: 'mark_read',
        arguments: {
          message_ids: [DISCORD_ENVELOPE.id, IMESSAGE_ENVELOPE.id],
        },
      });

      const call = harness.adapter.calls.at(-1)!;
      expect(call.method).toBe('POST');
      expect(new URL(call.url).pathname).toBe('/messages/mark_read');
      expect(JSON.parse(call.body!)).toEqual({
        message_ids: [DISCORD_ENVELOPE.id, IMESSAGE_ENVELOPE.id],
      });
      // mark_read is naturally idempotent at the storage layer; the wrapper
      // intentionally does NOT attach an Idempotency-Key.
      expect(getHeader(call, 'Idempotency-Key')).toBeUndefined();

      const payload = parseFirstTextBlock(result as { content: Array<{ type: string; text?: string }> }) as {
        marked_count: number;
      };
      expect(payload.marked_count).toBe(2);
    });

    it('rejects malformed message ids at the wrapper boundary (zod input schema)', async () => {
      // Note: McpServer's runtime validates the input against the registered
      // schema BEFORE invoking our handler, so this fails without ever hitting
      // the mock adapter.
      const result = (await harness.client.callTool({
        name: 'mark_read',
        arguments: { message_ids: ['not-a-msg-id'] },
      })) as { isError?: boolean; content?: Array<{ type: string; text: string }> };

      expect(result.isError).toBe(true);
      // Adapter must NOT have been called.
      expect(
        harness.adapter.calls.filter((c) => new URL(c.url).pathname === '/messages/mark_read'),
      ).toHaveLength(0);
    });
  });

  describe('get_message_context', () => {
    it('forwards channel_id + around_message_id + before/after to GET /messages/context', async () => {
      harness.adapter.enqueue(
        '/messages/context',
        jsonResponse({ messages: [DISCORD_ENVELOPE] }),
      );

      const result = await harness.client.callTool({
        name: 'get_message_context',
        arguments: {
          channel_id: 'discord:1234567890',
          around_message_id: DISCORD_ENVELOPE.id,
          before: 3,
          after: 2,
        },
      });

      const call = harness.adapter.calls.at(-1)!;
      expect(call.method).toBe('GET');
      const url = new URL(call.url);
      expect(url.pathname).toBe('/messages/context');
      expect(url.searchParams.get('channel_id')).toBe('discord:1234567890');
      expect(url.searchParams.get('around_message_id')).toBe(DISCORD_ENVELOPE.id);
      expect(url.searchParams.get('before')).toBe('3');
      expect(url.searchParams.get('after')).toBe('2');

      const payload = parseFirstTextBlock(result as { content: Array<{ type: string; text?: string }> }) as {
        messages: Array<{ id: string }>;
      };
      expect(payload.messages).toHaveLength(1);
      expect(payload.messages[0]!.id).toBe(DISCORD_ENVELOPE.id);
    });

    it('applies before=5/after=5 defaults when caller omits them (iMessage envelope)', async () => {
      harness.adapter.enqueue(
        '/messages/context',
        jsonResponse({ messages: [IMESSAGE_ENVELOPE] }),
      );

      await harness.client.callTool({
        name: 'get_message_context',
        arguments: {
          channel_id: '+15551234567',
          around_message_id: IMESSAGE_ENVELOPE.id,
        },
      });

      const url = new URL(harness.adapter.calls.at(-1)!.url);
      expect(url.searchParams.get('before')).toBe('5');
      expect(url.searchParams.get('after')).toBe('5');
    });
  });

  describe('end-to-end happy path: receive (Discord) → context → send → mark_read', () => {
    it('walks the agent loop without re-initializing the MCP session', async () => {
      // 1. list_unread — agent sees a Discord message.
      harness.adapter.enqueue(
        '/messages/unread',
        jsonResponse({ messages: [DISCORD_ENVELOPE], next_since: '2026-04-25T15:33:00Z' }),
      );
      const unread = await harness.client.callTool({
        name: 'list_unread_messages',
        arguments: {},
      });
      const unreadPayload = parseFirstTextBlock(
        unread as { content: Array<{ type: string; text?: string }> },
      ) as { messages: Array<{ id: string; channel_id: string }> };
      expect(unreadPayload.messages).toHaveLength(1);
      const msgId = unreadPayload.messages[0]!.id;
      const channelId = unreadPayload.messages[0]!.channel_id;

      // 2. get_message_context — agent pulls thread context.
      harness.adapter.enqueue(
        '/messages/context',
        jsonResponse({ messages: [DISCORD_ENVELOPE] }),
      );
      await harness.client.callTool({
        name: 'get_message_context',
        arguments: { channel_id: channelId, around_message_id: msgId },
      });

      // 3. send_message — agent replies.
      harness.adapter.enqueue(
        '/messages/send',
        jsonResponse({
          message_id: 'msg_01HZZZABCDEFGHJKMNPQRSTVWZ',
          sent_at: '2026-04-25T15:36:00Z',
        }),
      );
      await harness.client.callTool({
        name: 'send_message',
        arguments: { channel_id: channelId, text: 'on it' },
      });

      // 4. mark_read — agent acks the original message.
      harness.adapter.enqueue('/messages/mark_read', jsonResponse({ marked_count: 1 }));
      const ack = await harness.client.callTool({
        name: 'mark_read',
        arguments: { message_ids: [msgId] },
      });
      const ackPayload = parseFirstTextBlock(
        ack as { content: Array<{ type: string; text?: string }> },
      ) as { marked_count: number };
      expect(ackPayload.marked_count).toBe(1);

      // Sanity: every call carried the wrapper's identity headers.
      const paths = harness.adapter.calls.map((c) => new URL(c.url).pathname);
      expect(paths).toEqual([
        '/messages/unread',
        '/messages/context',
        '/messages/send',
        '/messages/mark_read',
      ]);
      for (const call of harness.adapter.calls) {
        expect(getHeader(call, 'Authorization')).toBe(`Bearer ${TEST_CONFIG.bearerToken}`);
        expect(getHeader(call, 'X-Agent-ID')).toBe(TEST_CONFIG.agentId);
      }
    });
  });
});
