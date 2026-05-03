// Wrapper config loader. Mirrors the load/get/reset pattern used by the
// Python adapter's `amc/core/auth.py` so tests can swap config in and out.
//
// Validation runs only when `loadConfig()` is called explicitly (typically
// from the entrypoint via `runStdio`). Importing this module never reads or
// validates env vars — that lets unit tests load the module without needing
// the production env set.
//
// Per spec §5.6 the wrapper takes three env vars:
//   - AMC_BASE_URL     (optional, default http://127.0.0.1:8080)
//   - AMC_BEARER_TOKEN (required)
//   - AMC_AGENT_ID     (required)
//
// Cross-runtime: only `process.env` is read. Works under Node 20+ and Bun.

export const ENV_BASE_URL = 'AMC_BASE_URL';
export const ENV_BEARER_TOKEN = 'AMC_BEARER_TOKEN';
export const ENV_AGENT_ID = 'AMC_AGENT_ID';

export const DEFAULT_BASE_URL = 'http://127.0.0.1:8080';

export interface WrapperConfig {
  readonly baseUrl: string;
  readonly bearerToken: string;
  readonly agentId: string;
}

/**
 * Raised when required env vars are missing or malformed. Subclassing Error so
 * that `instanceof Error` checks behave normally.
 */
export class WrapperConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'WrapperConfigError';
  }
}

let _configured: WrapperConfig | null = null;

/**
 * Read the three AMC_* env vars, validate them, and cache the result.
 *
 * If `env` is omitted, `process.env` is used. Pass an explicit env in tests.
 *
 * Throws `WrapperConfigError` if any required var is missing or empty after
 * trimming. Caller is expected to print the message to stderr and exit.
 */
export function loadConfig(
  env: Record<string, string | undefined> = process.env,
): WrapperConfig {
  const baseUrlRaw = env[ENV_BASE_URL];
  const tokenRaw = env[ENV_BEARER_TOKEN];
  const agentRaw = env[ENV_AGENT_ID];

  const baseUrl = (baseUrlRaw ?? '').trim() || DEFAULT_BASE_URL;
  const bearerToken = (tokenRaw ?? '').trim();
  const agentId = (agentRaw ?? '').trim();

  const missing: string[] = [];
  if (!bearerToken) missing.push(ENV_BEARER_TOKEN);
  if (!agentId) missing.push(ENV_AGENT_ID);

  if (missing.length > 0) {
    throw new WrapperConfigError(
      `Missing required env var(s): ${missing.join(', ')}. ` +
        `Set them before launching the MCP wrapper.`,
    );
  }

  // Cheap URL validity guard so a typo here surfaces at startup, not as a
  // confusing fetch error inside every tool call.
  try {
    new URL(baseUrl);
  } catch {
    throw new WrapperConfigError(
      `${ENV_BASE_URL} is not a valid URL: ${JSON.stringify(baseUrl)}`,
    );
  }

  _configured = { baseUrl, bearerToken, agentId };
  return _configured;
}

/**
 * Return the cached config. Throws if `loadConfig()` has not been called.
 */
export function getConfig(): WrapperConfig {
  if (_configured === null) {
    throw new WrapperConfigError(
      'Wrapper config has not been loaded. Call loadConfig() before getConfig().',
    );
  }
  return _configured;
}

/** Test-only: clear the cached config. */
export function resetConfig(): void {
  _configured = null;
}
