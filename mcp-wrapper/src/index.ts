#!/usr/bin/env node
// Entry point for the AMC MCP wrapper.
//
// This is a stub server: it registers zero tools and exists only to prove the
// stdio transport is wired up correctly. The four real tools
// (list_unread_messages, send_message, mark_read, get_message_context) are
// added in tasks #60-#63 per spec §5.6 / §7.4.7.
//
// Compatible with both Node 20+ and Bun: imports use only `node:`-prefixed
// stdlib + `@modelcontextprotocol/sdk`. No platform-specific deps.

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

import { SERVER_NAME, SERVER_VERSION } from './version.js';

export interface CreateServerOptions {
  name?: string;
  version?: string;
}

/**
 * Construct the MCP server with no tools registered.
 *
 * Exposed for tests so a smoke test can instantiate the server without
 * touching real stdio.
 */
export function createServer(options: CreateServerOptions = {}): Server {
  const name = options.name ?? SERVER_NAME;
  const version = options.version ?? SERVER_VERSION;

  return new Server(
    {
      name,
      version,
    },
    {
      // No capabilities advertised yet; tool capability is added in #60-#63.
      capabilities: {},
    },
  );
}

/**
 * Connect the server to the stdio transport. Resolves once the transport is
 * listening and the server is ready to handle the `initialize` handshake.
 */
export async function runStdio(server: Server): Promise<StdioServerTransport> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  return transport;
}

// Run only when invoked as a script (i.e. via the bin shim or `node dist/index.js`).
// import.meta.url comparison works on both Node and Bun.
const isMain = (() => {
  if (typeof process === 'undefined' || !process.argv[1]) {
    return false;
  }
  try {
    const invokedUrl = new URL(`file://${process.argv[1]}`).href;
    return import.meta.url === invokedUrl;
  } catch {
    return false;
  }
})();

if (isMain) {
  const server = createServer();
  runStdio(server).catch((error: unknown) => {
    // Write to stderr because stdout is reserved for the MCP framed protocol.
    process.stderr.write(
      `[amc-mcp] fatal: failed to start stdio server: ${
        error instanceof Error ? error.message : String(error)
      }\n`,
    );
    process.exit(1);
  });
}
