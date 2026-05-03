// Unit tests for src/config.ts.
//
// Verifies the load/get/reset cache pattern and that missing required env
// vars produce a clear failure (which the entrypoint will print to stderr).

import { afterEach, describe, expect, it } from 'vitest';

import {
  DEFAULT_BASE_URL,
  ENV_AGENT_ID,
  ENV_BASE_URL,
  ENV_BEARER_TOKEN,
  WrapperConfigError,
  getConfig,
  loadConfig,
  resetConfig,
} from '../src/config.js';

afterEach(() => {
  resetConfig();
});

describe('loadConfig', () => {
  it('returns a config when both required vars are present', () => {
    const cfg = loadConfig({
      [ENV_BEARER_TOKEN]: 'secret-token',
      [ENV_AGENT_ID]: 'claude-code',
    });
    expect(cfg.bearerToken).toBe('secret-token');
    expect(cfg.agentId).toBe('claude-code');
    expect(cfg.baseUrl).toBe(DEFAULT_BASE_URL);
  });

  it('uses the default base URL when AMC_BASE_URL is unset', () => {
    const cfg = loadConfig({
      [ENV_BEARER_TOKEN]: 't',
      [ENV_AGENT_ID]: 'a',
    });
    expect(cfg.baseUrl).toBe('http://127.0.0.1:8080');
  });

  it('honors AMC_BASE_URL when set', () => {
    const cfg = loadConfig({
      [ENV_BASE_URL]: 'http://localhost:9999',
      [ENV_BEARER_TOKEN]: 't',
      [ENV_AGENT_ID]: 'a',
    });
    expect(cfg.baseUrl).toBe('http://localhost:9999');
  });

  it('trims surrounding whitespace from values', () => {
    const cfg = loadConfig({
      [ENV_BASE_URL]: '  http://example.test  ',
      [ENV_BEARER_TOKEN]: '  token  ',
      [ENV_AGENT_ID]: '  agent  ',
    });
    expect(cfg.baseUrl).toBe('http://example.test');
    expect(cfg.bearerToken).toBe('token');
    expect(cfg.agentId).toBe('agent');
  });

  it('throws WrapperConfigError when AMC_BEARER_TOKEN is missing', () => {
    expect(() => loadConfig({ [ENV_AGENT_ID]: 'a' })).toThrow(WrapperConfigError);
    expect(() => loadConfig({ [ENV_AGENT_ID]: 'a' })).toThrow(/AMC_BEARER_TOKEN/);
  });

  it('throws WrapperConfigError when AMC_AGENT_ID is missing', () => {
    expect(() => loadConfig({ [ENV_BEARER_TOKEN]: 't' })).toThrow(WrapperConfigError);
    expect(() => loadConfig({ [ENV_BEARER_TOKEN]: 't' })).toThrow(/AMC_AGENT_ID/);
  });

  it('lists every missing var in a single error message', () => {
    let caught: unknown;
    try {
      loadConfig({});
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(WrapperConfigError);
    const msg = (caught as Error).message;
    expect(msg).toContain('AMC_BEARER_TOKEN');
    expect(msg).toContain('AMC_AGENT_ID');
  });

  it('treats whitespace-only values as missing', () => {
    expect(() =>
      loadConfig({
        [ENV_BEARER_TOKEN]: '   ',
        [ENV_AGENT_ID]: 'a',
      }),
    ).toThrow(/AMC_BEARER_TOKEN/);
  });

  it('rejects an invalid AMC_BASE_URL', () => {
    expect(() =>
      loadConfig({
        [ENV_BASE_URL]: 'not a url',
        [ENV_BEARER_TOKEN]: 't',
        [ENV_AGENT_ID]: 'a',
      }),
    ).toThrow(/AMC_BASE_URL/);
  });
});

describe('getConfig', () => {
  it('returns the cached config after loadConfig', () => {
    loadConfig({
      [ENV_BEARER_TOKEN]: 't',
      [ENV_AGENT_ID]: 'a',
    });
    expect(getConfig().bearerToken).toBe('t');
  });

  it('throws if loadConfig has not been called', () => {
    expect(() => getConfig()).toThrow(WrapperConfigError);
  });

  it('resetConfig clears the cache', () => {
    loadConfig({
      [ENV_BEARER_TOKEN]: 't',
      [ENV_AGENT_ID]: 'a',
    });
    resetConfig();
    expect(() => getConfig()).toThrow();
  });
});
