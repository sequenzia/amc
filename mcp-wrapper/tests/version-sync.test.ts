// Guards that src/version.ts stays in sync with package.json so the MCP
// `serverInfo.version` reported to clients matches the published bin version.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { SERVER_NAME, SERVER_VERSION } from '../src/version.js';

const here = dirname(fileURLToPath(import.meta.url));
const pkgPath = resolve(here, '..', 'package.json');
const pkg = JSON.parse(readFileSync(pkgPath, 'utf8')) as {
  name: string;
  version: string;
};

describe('version.ts is in sync with package.json', () => {
  it('SERVER_NAME matches package.json name', () => {
    expect(SERVER_NAME).toBe(pkg.name);
  });

  it('SERVER_VERSION matches package.json version', () => {
    expect(SERVER_VERSION).toBe(pkg.version);
  });
});
