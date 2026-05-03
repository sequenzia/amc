// Unit tests for the standalone `scripts/import-audit.mjs` static check.
//
// We exercise the script two ways:
//   1. As a child process via `node scripts/import-audit.mjs <dist-dir>`,
//      asserting exit codes against fixture trees.
//   2. By importing its exported helpers (`extractImportSpecifiers`,
//      `findForbiddenToken`) to pin down the import-statement-aware behavior
//      that distinguishes it from the substring-grep test in #58.

import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

// @ts-expect-error — .mjs script with no .d.ts; we test runtime behavior.
import {
  auditDist,
  extractImportSpecifiers,
  findForbiddenToken,
} from '../scripts/import-audit.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const scriptPath = resolve(here, '..', 'scripts', 'import-audit.mjs');

function runAudit(distDir: string): { status: number | null; stdout: string; stderr: string } {
  const result = spawnSync(process.execPath, [scriptPath, distDir], {
    encoding: 'utf8',
  });
  return { status: result.status, stdout: result.stdout, stderr: result.stderr };
}

describe('import-audit.mjs — script exit codes', () => {
  let tmpRoot: string;
  let distDir: string;

  beforeEach(() => {
    tmpRoot = mkdtempSync(join(tmpdir(), 'amc-import-audit-'));
    distDir = join(tmpRoot, 'dist');
    mkdirSync(distDir, { recursive: true });
  });

  afterEach(() => {
    rmSync(tmpRoot, { recursive: true, force: true });
  });

  it('exits 0 on a clean dist tree', () => {
    writeFileSync(
      join(distDir, 'good.js'),
      `import { Server } from '@modelcontextprotocol/sdk/server/index.js';\nexport const x = 1;\n`,
    );
    const { status, stdout } = runAudit(distDir);
    expect(status).toBe(0);
    expect(stdout).toContain('OK');
  });

  it('exits 1 when a forbidden import is present', () => {
    writeFileSync(
      join(distDir, 'good.js'),
      `import { Server } from '@modelcontextprotocol/sdk/server/index.js';\n`,
    );
    writeFileSync(
      join(distDir, 'bad.js'),
      `import { Client } from 'discord.js';\nexport const y = 2;\n`,
    );
    const { status, stderr } = runAudit(distDir);
    expect(status).toBe(1);
    expect(stderr).toContain('FAIL');
    expect(stderr).toContain('bad.js');
    expect(stderr).toContain('discord.js');
    expect(stderr).toContain('discord');
  });

  it('exits 1 for each forbidden token category', () => {
    // One file per token to confirm all five are in scope.
    writeFileSync(join(distDir, 'a.js'), `import 'discord.js';\n`);
    writeFileSync(join(distDir, 'b.js'), `import 'node-applescript';\n`);
    writeFileSync(join(distDir, 'c.js'), `require('osascript-runner');\n`);
    writeFileSync(join(distDir, 'd.js'), `import('./readers/chat.db.js');\n`);
    writeFileSync(join(distDir, 'e.js'), `export * from './imessage/poller.js';\n`);
    const { status, stderr } = runAudit(distDir);
    expect(status).toBe(1);
    expect(stderr).toContain('"discord"');
    expect(stderr).toContain('"applescript"');
    expect(stderr).toContain('"osascript"');
    expect(stderr).toContain('"chat.db"');
    expect(stderr).toContain('"imessage"');
  });

  it('exits 2 when the dist directory does not exist', () => {
    const missing = join(tmpRoot, 'does-not-exist');
    const { status, stderr } = runAudit(missing);
    expect(status).toBe(2);
    expect(stderr).toContain('not found');
  });

  it('recurses into subdirectories', () => {
    const nested = join(distDir, 'nested', 'deeper');
    mkdirSync(nested, { recursive: true });
    writeFileSync(join(nested, 'bad.js'), `import { Foo } from 'discord-api-types';\n`);
    const { status, stderr } = runAudit(distDir);
    expect(status).toBe(1);
    expect(stderr).toContain('discord-api-types');
  });

  it('ignores non-.js files', () => {
    writeFileSync(join(distDir, 'good.js'), `export const x = 1;\n`);
    // .d.ts and .map and .json files mention forbidden tokens — they must not
    // trip the audit because tsc emits them alongside .js.
    writeFileSync(
      join(distDir, 'malicious.d.ts'),
      `export { Client } from 'discord.js';\n`,
    );
    writeFileSync(join(distDir, 'good.js.map'), `{"sources":["discord.ts"]}\n`);
    const { status } = runAudit(distDir);
    expect(status).toBe(0);
  });
});

describe('import-audit.mjs — import-statement-aware (vs substring grep)', () => {
  it('does NOT flag the word "Discord" inside a string literal', () => {
    // This is the precise false-positive the task description calls out:
    // a tool description string that mentions "Discord" must not trip the
    // audit, because it is not an import path.
    const source = `
      export const TOOL_DESCRIPTION = 'Send a message to Discord or iMessage.';
      import { Server } from '@modelcontextprotocol/sdk/server/index.js';
    `;
    const specifiers = extractImportSpecifiers(source).map(
      (h: { specifier: string }) => h.specifier,
    );
    expect(specifiers).toEqual(['@modelcontextprotocol/sdk/server/index.js']);
    expect(findForbiddenToken('@modelcontextprotocol/sdk/server/index.js')).toBeNull();
  });

  it('captures every standard import form tsc can emit', () => {
    const source = [
      `import 'a';`,
      `import x from 'b';`,
      `import { y } from 'c';`,
      `import * as ns from 'd';`,
      `import x2, { z } from 'e';`,
      `export { foo } from 'f';`,
      `export * from 'g';`,
      `export * as star from 'h';`,
      `await import('i');`,
      `const m = require('j');`,
    ].join('\n');
    const specs = extractImportSpecifiers(source).map(
      (h: { specifier: string }) => h.specifier,
    );
    // Order is not guaranteed (multiple regexes), so compare as a set.
    expect(new Set(specs)).toEqual(new Set(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j']));
  });

  it('matches forbidden tokens case-insensitively', () => {
    expect(findForbiddenToken('Discord.js')).toBe('discord');
    expect(findForbiddenToken('node-AppleScript')).toBe('applescript');
    expect(findForbiddenToken('@org/iMessage-poller')).toBe('imessage');
    expect(findForbiddenToken('@modelcontextprotocol/sdk/server/index.js')).toBeNull();
  });

  it('auditDist returns structured violations', () => {
    const dir = mkdtempSync(join(tmpdir(), 'amc-import-audit-api-'));
    try {
      const distDir = join(dir, 'dist');
      mkdirSync(distDir);
      writeFileSync(join(distDir, 'bad.js'), `import 'discord.js';\n`);
      const violations = auditDist(distDir);
      expect(violations).toHaveLength(1);
      expect(violations[0]).toMatchObject({
        specifier: 'discord.js',
        token: 'discord',
        line: 1,
      });
      expect(violations[0].file).toContain('bad.js');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
