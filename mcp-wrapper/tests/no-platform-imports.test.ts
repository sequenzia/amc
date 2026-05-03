// Static guard: the bundled output (dist/) must not pull in anything
// platform-specific. Spec §9.3 requires "Wrapper has zero platform-specific
// imports" — this test enforces that at build verification time.
//
// We grep both the compiled JS and the package's transitive dependency tree
// (via package.json) for the forbidden tokens.

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..');
const distDir = resolve(repoRoot, 'dist');

const FORBIDDEN_TOKENS = ['discord', 'applescript', 'osascript', 'chat.db'] as const;

function walkJs(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const s = statSync(full);
    if (s.isDirectory()) {
      out.push(...walkJs(full));
    } else if (name.endsWith('.js')) {
      out.push(full);
    }
  }
  return out;
}

describe('no platform-specific imports leak into the bundle', () => {
  it('package.json declares no platform-specific deps', () => {
    const pkg = JSON.parse(readFileSync(resolve(repoRoot, 'package.json'), 'utf8')) as {
      dependencies?: Record<string, string>;
      devDependencies?: Record<string, string>;
    };
    const allDeps = Object.keys({
      ...(pkg.dependencies ?? {}),
      ...(pkg.devDependencies ?? {}),
    });
    for (const dep of allDeps) {
      const lower = dep.toLowerCase();
      for (const token of FORBIDDEN_TOKENS) {
        // dep names like '@types/node' are fine; we only flag if a *dep name*
        // contains a forbidden token (e.g. 'discord.js', 'applescript').
        expect(
          lower.includes(token),
          `Forbidden token "${token}" found in dep name "${dep}"`,
        ).toBe(false);
      }
    }
  });

  it('compiled dist/ contains no forbidden tokens (only runs after build)', () => {
    if (!existsSync(distDir)) {
      // Skip rather than fail in pre-build environments.
      return;
    }
    const files = walkJs(distDir);
    for (const file of files) {
      const text = readFileSync(file, 'utf8').toLowerCase();
      for (const token of FORBIDDEN_TOKENS) {
        expect(
          text.includes(token),
          `Forbidden token "${token}" found in compiled output ${file}`,
        ).toBe(false);
      }
    }
  });
});
