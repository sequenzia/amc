#!/usr/bin/env node
// Static guard: scan the built `dist/` tree for forbidden platform-specific
// module imports. Spec §9.3 requires "Wrapper has zero platform-specific
// imports" — this script enforces that at build verification time, runnable
// outside vitest so CI / hand-checks can invoke it directly.
//
// Usage:
//   node scripts/import-audit.mjs [dist-dir]
//
// Default dist-dir is `<package-root>/dist`. The companion vitest test
// `tests/no-platform-imports.test.ts` (#58) does a substring-level grep over
// the whole compiled file; this script narrows that to the actual import
// path so a tool description string mentioning e.g. "Discord" doesn't trip
// the audit.
//
// Exit codes:
//   0 — clean
//   1 — one or more forbidden imports found (printed to stderr)
//   2 — usage / IO error (e.g. dist dir missing)

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const FORBIDDEN_TOKENS = ['discord', 'applescript', 'osascript', 'chat.db', 'imessage'];

// Regexes that capture the module path (group 1) of every JS import form
// `tsc` can emit. We intentionally match only on the *module specifier* and
// never on arbitrary string literals.
//
// Covered forms:
//   import 'mod';
//   import x from 'mod';
//   import { a, b } from 'mod';
//   import * as ns from 'mod';
//   import x, { y } from 'mod';
//   export { a } from 'mod';
//   export * from 'mod';
//   export * as ns from 'mod';
//   await import('mod');                        — dynamic, statically analyzable
//   require('mod');                              — interop / CJS shim emitted by tsc
const IMPORT_PATTERNS = [
  /\bimport\s+(?:[\w*${}\s,]+\s+from\s+)?['"]([^'"]+)['"]/g,
  /\bexport\s+(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s+from\s+['"]([^'"]+)['"]/g,
  /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
  /\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
];

function walkJs(dir) {
  const out = [];
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

/**
 * Extract import module specifiers from a JS source string. Returns each
 * specifier paired with the 1-based line it appeared on (best-effort, used
 * only for human-readable error output).
 */
export function extractImportSpecifiers(source) {
  const hits = [];
  for (const pattern of IMPORT_PATTERNS) {
    // Reset lastIndex defensively — these are module-level regexes with /g.
    pattern.lastIndex = 0;
    let match;
    while ((match = pattern.exec(source)) !== null) {
      const specifier = match[1];
      const line = source.slice(0, match.index).split('\n').length;
      hits.push({ specifier, line });
    }
  }
  return hits;
}

/**
 * Check a single specifier against the forbidden-token list. Returns the
 * matched token, or `null` if clean. Comparison is case-insensitive to match
 * the no-platform-imports.test.ts behavior.
 */
export function findForbiddenToken(specifier, tokens = FORBIDDEN_TOKENS) {
  const lower = specifier.toLowerCase();
  for (const token of tokens) {
    if (lower.includes(token)) {
      return token;
    }
  }
  return null;
}

/**
 * Audit a directory of compiled `.js` files. Returns the list of violations.
 * Each violation: { file, specifier, token, line }.
 */
export function auditDist(distDir) {
  const violations = [];
  const files = walkJs(distDir);
  for (const file of files) {
    const source = readFileSync(file, 'utf8');
    for (const { specifier, line } of extractImportSpecifiers(source)) {
      const token = findForbiddenToken(specifier);
      if (token !== null) {
        violations.push({ file, specifier, token, line });
      }
    }
  }
  return violations;
}

function defaultDistDir() {
  const here = dirname(fileURLToPath(import.meta.url));
  // scripts/ sits under the package root next to dist/.
  return resolve(here, '..', 'dist');
}

function main(argv) {
  const explicit = argv[2];
  const distDir = explicit ? resolve(explicit) : defaultDistDir();

  if (!existsSync(distDir)) {
    process.stderr.write(
      `[import-audit] dist directory not found: ${distDir}\n` +
        `Run \`npm run build\` first, or pass a path: ` +
        `node scripts/import-audit.mjs <dist-dir>\n`,
    );
    return 2;
  }

  const stat = statSync(distDir);
  if (!stat.isDirectory()) {
    process.stderr.write(`[import-audit] not a directory: ${distDir}\n`);
    return 2;
  }

  const violations = auditDist(distDir);
  if (violations.length === 0) {
    process.stdout.write(`[import-audit] OK — no forbidden imports under ${distDir}\n`);
    return 0;
  }

  process.stderr.write(
    `[import-audit] FAIL — ${violations.length} forbidden import(s) found:\n`,
  );
  for (const v of violations) {
    process.stderr.write(
      `  ${v.file}:${v.line}  imports "${v.specifier}"  (matched token: "${v.token}")\n`,
    );
  }
  return 1;
}

// Run only when invoked as a script, not when imported by tests.
const invokedAsScript = (() => {
  if (typeof process === 'undefined' || !process.argv[1]) return false;
  try {
    return import.meta.url === new URL(`file://${process.argv[1]}`).href;
  } catch {
    return false;
  }
})();

if (invokedAsScript) {
  process.exit(main(process.argv));
}
