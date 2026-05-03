// Smoke test: import the entry module, construct the server, and confirm the
// initialize handshake works against an in-memory transport pair. Verifies the
// stub server boots without crashing and answers `initialize`.

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { describe, expect, it } from 'vitest';

import { createServer, runStdio } from '../src/index.js';
import { SERVER_NAME, SERVER_VERSION } from '../src/version.js';

const here = dirname(fileURLToPath(import.meta.url));
const distEntry = resolve(here, '..', 'dist', 'index.js');

describe('amc-mcp-wrapper entry', () => {
  it('exports createServer + runStdio', () => {
    expect(typeof createServer).toBe('function');
    expect(typeof runStdio).toBe('function');
  });

  it('createServer returns a Server instance with the expected identity', () => {
    const server = createServer();
    expect(server).toBeDefined();
    // The Server class does not expose serverInfo publicly; we trust the
    // initialize handshake below to assert identity.
    expect(server.constructor.name).toBe('Server');
  });

  it('responds to initialize over an in-memory transport', async () => {
    const server = createServer();
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    await server.connect(serverTransport);

    const client = new Client({ name: 'amc-mcp-test-client', version: '0.0.0' });
    await client.connect(clientTransport);

    const serverVersion = client.getServerVersion();
    expect(serverVersion).toBeDefined();
    expect(serverVersion?.name).toBe(SERVER_NAME);
    expect(serverVersion?.version).toBe(SERVER_VERSION);

    // No tools registered yet (#60-#63 add them). Server advertises no `tools`
    // capability, so it intentionally rejects `tools/list` with -32601.
    const serverCapabilities = client.getServerCapabilities();
    expect(serverCapabilities?.tools).toBeUndefined();

    // ping() is part of the base protocol and should round-trip.
    await expect(client.ping()).resolves.toBeDefined();

    await client.close();
    await server.close();
  });
});

describe('amc-mcp-wrapper bin (built dist/index.js)', () => {
  it('boots over real stdio and answers initialize (only when built)', async () => {
    if (!existsSync(distEntry)) {
      // dist/index.js is produced by `npm run build`. The smoke test is still
      // valuable in pre-build dev loops (above), so skip rather than fail.
      return;
    }

    const child = spawn(process.execPath, [distEntry], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    try {
      const initRequest =
        JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'initialize',
          params: {
            protocolVersion: '2025-03-26',
            capabilities: {},
            clientInfo: { name: 'amc-mcp-smoke', version: '0.0.0' },
          },
        }) + '\n';

      const response = await new Promise<string>((resolvePromise, rejectPromise) => {
        const timer = setTimeout(() => {
          rejectPromise(new Error('timed out waiting for initialize response'));
        }, 5000);

        let buffer = '';
        child.stdout.on('data', (chunk: Buffer) => {
          buffer += chunk.toString('utf8');
          const newlineIdx = buffer.indexOf('\n');
          if (newlineIdx !== -1) {
            clearTimeout(timer);
            resolvePromise(buffer.slice(0, newlineIdx));
          }
        });
        child.on('error', (err) => {
          clearTimeout(timer);
          rejectPromise(err);
        });
        child.on('exit', (code) => {
          clearTimeout(timer);
          rejectPromise(new Error(`child exited early with code ${code}`));
        });

        child.stdin.write(initRequest);
      });

      const parsed = JSON.parse(response) as {
        jsonrpc: string;
        id: number;
        result?: { serverInfo?: { name: string; version: string } };
        error?: unknown;
      };
      expect(parsed.jsonrpc).toBe('2.0');
      expect(parsed.id).toBe(1);
      expect(parsed.error).toBeUndefined();
      expect(parsed.result?.serverInfo?.name).toBe(SERVER_NAME);
      expect(parsed.result?.serverInfo?.version).toBe(SERVER_VERSION);
    } finally {
      child.kill('SIGTERM');
    }
  });
});
