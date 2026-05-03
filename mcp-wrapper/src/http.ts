// Shared HTTP client for the four MCP tools.
//
// Spec references:
//   - §5.6 — wrapper config (AMC_BASE_URL / AMC_BEARER_TOKEN / AMC_AGENT_ID)
//   - §7.4 header table — Bearer + X-Agent-ID on every call;
//                         Idempotency-Key recommended on POST /messages/send
//   - §7.4.12 — standard error envelope: { error: { code, message, details? } }
//
// Design contract:
//   * Uses stdlib `fetch` only (Node 20+ / Bun). No third-party HTTP dep.
//   * Caller-provided `extraHeaders` win over defaults — this is how
//     `send_message` injects its per-call `Idempotency-Key`. The wrapper
//     itself never auto-generates the key; callers do (so non-send tools
//     don't accidentally get one and so tests can pin it).
//   * `get` and `post` always return a discriminated `HttpResult<T>`.
//     They never throw on HTTP errors or transport failures — the four tool
//     handlers translate the result via the error mapper added in #64.
//   * `randomIdempotencyKey()` is exported as the canonical key generator so
//     `send_message` (#61) imports it from one place.

import { randomUUID } from 'node:crypto';

import { getConfig, type WrapperConfig } from './config.js';

// Derive standard fetch types from the global `fetch` function so we don't
// depend on DOM lib globals being present in every lint config.
type FetchFn = typeof fetch;
type FetchResponse = Awaited<ReturnType<FetchFn>>;

/** A primitive that we know how to render into a query string. */
export type QueryValue = string | number | boolean | null | undefined;

/** Map of query parameter name -> value (or array for repeated params). */
export type QueryParams = Record<string, QueryValue | readonly QueryValue[]>;

/** Headers added on top of the defaults for a single call. */
export type ExtraHeaders = Record<string, string>;

/** Result returned to the four tool handlers. */
export type HttpResult<T = unknown> = HttpOk<T> | HttpErr;

export interface HttpOk<T> {
  readonly ok: true;
  readonly status: number;
  readonly data: T;
}

export interface HttpErr {
  readonly ok: false;
  readonly status: number;
  /** Stable error code from §7.4.12, or `NETWORK_ERROR` for transport failures. */
  readonly code: string;
  readonly message: string;
  /**
   * Raw decoded response body when the server actually replied. Undefined for
   * transport-level failures (network error, malformed JSON, etc.).
   */
  readonly body?: unknown;
}

/** Synthetic status used when no HTTP response was received at all. */
export const NETWORK_ERROR_STATUS = 0;

export interface HttpClient {
  get<T = unknown>(path: string, query?: QueryParams): Promise<HttpResult<T>>;
  post<T = unknown>(
    path: string,
    body: unknown,
    extraHeaders?: ExtraHeaders,
  ): Promise<HttpResult<T>>;
}

interface ClientOptions {
  /** Override config (tests). Defaults to `getConfig()`. */
  config?: WrapperConfig;
  /** Override fetch (tests). Defaults to global `fetch`. */
  fetchImpl?: typeof fetch;
}

/**
 * UUID v4 string suitable for an `Idempotency-Key` header. Exported so the
 * `send_message` tool can request one explicitly per spec §7.4.7.
 */
export function randomIdempotencyKey(): string {
  return randomUUID();
}

/**
 * Construct a client bound to a config. The default factory pulls config from
 * `getConfig()` (which requires `loadConfig()` to have been called at startup
 * — see config.ts). Tests can pass an explicit config and a stubbed fetch.
 */
export function createHttpClient(options: ClientOptions = {}): HttpClient {
  const config = options.config ?? getConfig();
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;

  if (typeof fetchImpl !== 'function') {
    // Should never happen on Node 20+/Bun, but fail loud if it does.
    throw new Error('Global fetch is not available in this runtime');
  }

  const baseUrl = stripTrailingSlash(config.baseUrl);

  function buildUrl(path: string, query?: QueryParams): string {
    const normalizedPath = path.startsWith('/') ? path : `/${path}`;
    const url = new URL(baseUrl + normalizedPath);
    if (query) {
      appendQuery(url.searchParams, query);
    }
    return url.toString();
  }

  function defaultHeaders(): Record<string, string> {
    return {
      Authorization: `Bearer ${config.bearerToken}`,
      'X-Agent-ID': config.agentId,
      'Content-Type': 'application/json',
    };
  }

  async function request<T>(
    method: 'GET' | 'POST',
    url: string,
    body: unknown,
    extraHeaders?: ExtraHeaders,
  ): Promise<HttpResult<T>> {
    const headers = { ...defaultHeaders(), ...(extraHeaders ?? {}) };

    let response: FetchResponse;
    try {
      response = await fetchImpl(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (err) {
      return {
        ok: false,
        status: NETWORK_ERROR_STATUS,
        code: 'NETWORK_ERROR',
        message: err instanceof Error ? err.message : String(err),
      };
    }

    return parseFetchResponse<T>(response);
  }

  return {
    async get<T = unknown>(path: string, query?: QueryParams): Promise<HttpResult<T>> {
      return request<T>('GET', buildUrl(path, query), undefined);
    },
    async post<T = unknown>(
      path: string,
      body: unknown,
      extraHeaders?: ExtraHeaders,
    ): Promise<HttpResult<T>> {
      return request<T>('POST', buildUrl(path), body, extraHeaders);
    },
  };
}

function stripTrailingSlash(url: string): string {
  return url.endsWith('/') ? url.slice(0, -1) : url;
}

function appendQuery(params: URLSearchParams, query: QueryParams): void {
  for (const [key, raw] of Object.entries(query)) {
    if (raw === undefined || raw === null) continue;
    if (Array.isArray(raw)) {
      for (const item of raw) {
        if (item === undefined || item === null) continue;
        params.append(key, String(item));
      }
    } else {
      params.append(key, String(raw));
    }
  }
}

async function parseFetchResponse<T>(response: FetchResponse): Promise<HttpResult<T>> {
  const status = response.status;

  // 204 No Content (e.g., POST /typing) — no body to parse.
  if (status === 204) {
    if (status >= 200 && status < 300) {
      return { ok: true, status, data: undefined as T };
    }
  }

  const rawText = await safeReadText(response);
  const parsed = rawText === '' ? undefined : tryParseJson(rawText);

  if (status >= 200 && status < 300) {
    return { ok: true, status, data: parsed as T };
  }

  // Non-2xx: try to extract the spec §7.4.12 error envelope. Fall back to
  // INTERNAL_ERROR / status text when the body isn't shaped as expected.
  const { code, message } = extractError(parsed, response.statusText);
  return {
    ok: false,
    status,
    code,
    message,
    body: parsed,
  };
}

async function safeReadText(response: FetchResponse): Promise<string> {
  try {
    return await response.text();
  } catch {
    return '';
  }
}

function tryParseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function extractError(
  body: unknown,
  fallbackMessage: string,
): { code: string; message: string } {
  if (
    body !== null &&
    typeof body === 'object' &&
    'error' in body &&
    (body as { error: unknown }).error !== null &&
    typeof (body as { error: unknown }).error === 'object'
  ) {
    const err = (body as { error: Record<string, unknown> }).error;
    const code = typeof err.code === 'string' && err.code.length > 0 ? err.code : 'INTERNAL_ERROR';
    const message =
      typeof err.message === 'string' && err.message.length > 0
        ? err.message
        : fallbackMessage || 'Request failed';
    return { code, message };
  }
  return {
    code: 'INTERNAL_ERROR',
    message: fallbackMessage || 'Request failed',
  };
}
