// Unit tests for src/errors.ts.
//
// Covers every documented mapping (UNAUTHORIZED, AGENT_ID_REQUIRED,
// MESSAGE_NOT_FOUND, CHANNEL_NOT_FOUND, IDEMPOTENCY_KEY_REUSE, RATE_LIMITED,
// PLATFORM_SEND_FAILED, SEND_FAILED, PLATFORM_AUTH,
// ATTACHMENT_TOO_LARGE_FOR_PLATFORM, INTERNAL_ERROR, unmapped, NETWORK_ERROR).
//
// The mapper depends on `getConfig()` for the network-error branch, so each
// test that exercises that branch passes an explicit `resolveBaseUrl` override
// instead of touching the global config singleton.

import { afterEach, describe, expect, it } from 'vitest';

import {
  ENV_AGENT_ID,
  ENV_BASE_URL,
  ENV_BEARER_TOKEN,
  loadConfig,
  resetConfig,
} from '../src/config.js';
import { mapHttpErrorToMcpResponse } from '../src/errors.js';
import { NETWORK_ERROR_STATUS, type HttpErr } from '../src/http.js';

afterEach(() => {
  resetConfig();
});

function httpErr(partial: Partial<HttpErr> & Pick<HttpErr, 'code'>): HttpErr {
  return {
    ok: false,
    status: 500,
    message: '',
    ...partial,
  } as HttpErr;
}

function textOf(response: ReturnType<typeof mapHttpErrorToMcpResponse>): string {
  expect(response.isError).toBe(true);
  expect(response.content).toHaveLength(1);
  const entry = response.content[0]!;
  expect(entry.type).toBe('text');
  return entry.text;
}

describe('mapHttpErrorToMcpResponse — shape', () => {
  it('returns content array with isError:true', () => {
    const r = mapHttpErrorToMcpResponse(
      httpErr({ status: 401, code: 'UNAUTHORIZED', message: 'bad token' }),
    );
    expect(r.isError).toBe(true);
    expect(Array.isArray(r.content)).toBe(true);
    expect(r.content[0]!.type).toBe('text');
    expect(typeof r.content[0]!.text).toBe('string');
  });
});

describe('mapHttpErrorToMcpResponse — known codes', () => {
  it('401 UNAUTHORIZED', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 401, code: 'UNAUTHORIZED', message: 'bad token' }),
      ),
    );
    expect(text).toBe('Bearer token rejected by adapter. Check AMC_BEARER_TOKEN.');
  });

  it('400 AGENT_ID_REQUIRED', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 400, code: 'AGENT_ID_REQUIRED', message: 'missing X-Agent-ID' }),
      ),
    );
    expect(text).toBe('Internal error: agent id missing — please report.');
  });

  it('404 MESSAGE_NOT_FOUND', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 404, code: 'MESSAGE_NOT_FOUND', message: 'no such msg' }),
      ),
    );
    expect(text).toBe('Message not found or quarantined.');
  });

  it('404 CHANNEL_NOT_FOUND', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 404, code: 'CHANNEL_NOT_FOUND', message: 'unknown channel' }),
      ),
    );
    expect(text).toBe(
      'Channel not registered. Send to a channel that has received at ' +
        'least one inbound message.',
    );
  });

  it('422 IDEMPOTENCY_KEY_REUSE', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 422, code: 'IDEMPOTENCY_KEY_REUSE', message: 'reused key' }),
      ),
    );
    expect(text).toBe('Internal error: idempotency key collision — please retry.');
  });

  it('429 RATE_LIMITED with retry_after in body details', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({
          status: 429,
          code: 'RATE_LIMITED',
          message: 'too many',
          body: {
            error: {
              code: 'RATE_LIMITED',
              message: 'too many',
              details: { retry_after: 7 },
            },
          },
        }),
      ),
    );
    expect(text).toBe('Rate limited. Retry after 7 seconds.');
  });

  it('429 RATE_LIMITED with string retry_after value', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({
          status: 429,
          code: 'RATE_LIMITED',
          message: 'too many',
          body: {
            error: { code: 'RATE_LIMITED', message: 'too many', details: { retry_after: '12' } },
          },
        }),
      ),
    );
    expect(text).toBe('Rate limited. Retry after 12 seconds.');
  });

  it('429 RATE_LIMITED without retry_after falls back to a generic phrasing', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 429, code: 'RATE_LIMITED', message: 'too many' }),
      ),
    );
    expect(text).toMatch(/^Rate limited\./);
    expect(text).toMatch(/seconds\.$/);
  });

  it('502 PLATFORM_SEND_FAILED includes upstream message', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({
          status: 502,
          code: 'PLATFORM_SEND_FAILED',
          message: 'Discord 503 Service Unavailable',
        }),
      ),
    );
    expect(text).toBe('Platform delivery failed: Discord 503 Service Unavailable.');
  });

  it('502 SEND_FAILED is treated the same as PLATFORM_SEND_FAILED', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 502, code: 'SEND_FAILED', message: 'connection reset' }),
      ),
    );
    expect(text).toBe('Platform delivery failed: connection reset.');
  });

  it('502 PLATFORM_AUTH', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 502, code: 'PLATFORM_AUTH', message: 'discord token rejected' }),
      ),
    );
    expect(text).toBe('Platform authentication failed (e.g., Discord token rejected).');
  });

  it('413 ATTACHMENT_TOO_LARGE_FOR_PLATFORM', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({
          status: 413,
          code: 'ATTACHMENT_TOO_LARGE_FOR_PLATFORM',
          message: 'file is 30MB',
        }),
      ),
    );
    expect(text).toBe('Attachment exceeds platform limit. Compress or omit.');
  });

  it('500 INTERNAL_ERROR includes upstream message', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 500, code: 'INTERNAL_ERROR', message: 'unexpected exception' }),
      ),
    );
    expect(text).toBe('Adapter internal error: unexpected exception.');
  });

  it('falls back to INTERNAL_ERROR phrasing for an unknown code', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({ status: 503, code: 'TOTALLY_NEW_CODE', message: 'mystery failure' }),
      ),
    );
    expect(text).toBe('Adapter internal error: mystery failure.');
  });
});

describe('mapHttpErrorToMcpResponse — network errors', () => {
  it('NETWORK_ERROR uses configured AMC_BASE_URL via override', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({
          status: NETWORK_ERROR_STATUS,
          code: 'NETWORK_ERROR',
          message: 'fetch failed',
        }),
        { resolveBaseUrl: () => 'http://192.168.1.10:8080' },
      ),
    );
    expect(text).toBe('Cannot reach adapter at http://192.168.1.10:8080. Is it running?');
  });

  it('NETWORK_ERROR reads from getConfig() when no override is provided', () => {
    loadConfig({
      [ENV_BASE_URL]: 'http://localhost:9999',
      [ENV_BEARER_TOKEN]: 't',
      [ENV_AGENT_ID]: 'a',
    });
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({
          status: NETWORK_ERROR_STATUS,
          code: 'NETWORK_ERROR',
          message: 'connection refused',
        }),
      ),
    );
    expect(text).toBe('Cannot reach adapter at http://localhost:9999. Is it running?');
  });

  it('NETWORK_ERROR falls back to a placeholder when config is unloaded', () => {
    // resetConfig() runs in afterEach; ensure the singleton is empty here too.
    resetConfig();
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({
          status: NETWORK_ERROR_STATUS,
          code: 'NETWORK_ERROR',
          message: 'fetch failed',
        }),
      ),
    );
    expect(text).toBe('Cannot reach adapter at AMC_BASE_URL. Is it running?');
  });

  it('NETWORK_ERROR tolerates a throwing override', () => {
    const text = textOf(
      mapHttpErrorToMcpResponse(
        httpErr({
          status: NETWORK_ERROR_STATUS,
          code: 'NETWORK_ERROR',
          message: 'fetch failed',
        }),
        {
          resolveBaseUrl: () => {
            throw new Error('config not loaded');
          },
        },
      ),
    );
    expect(text).toBe('Cannot reach adapter at AMC_BASE_URL. Is it running?');
  });
});
