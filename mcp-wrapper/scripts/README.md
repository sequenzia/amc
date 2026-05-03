# mcp-wrapper scripts

Standalone scripts for the AMC MCP wrapper. These are invoked outside of
`vitest` so they can be run from CI, pre-commit hooks, or by hand.

## `import-audit.mjs`

Walks the compiled `dist/` tree and fails if any `.js` file imports a module
whose path contains a platform-specific token (`discord`, `applescript`,
`osascript`, `chat.db`, `imessage`). Spec §9.3 mandates "Wrapper has zero
platform-specific imports" — this script is the static enforcement.

Unlike the substring-level vitest companion at
`tests/no-platform-imports.test.ts`, this script is **import-statement-aware**:
it only inspects module specifiers, so a tool description string that
mentions e.g. "Discord" does not produce a false positive.

### Usage

```bash
# Audit the default ./dist directory (after `npm run build`):
node scripts/import-audit.mjs

# Audit an arbitrary dist tree (used by the unit test fixture):
node scripts/import-audit.mjs path/to/dist
```

### Exit codes

| Code | Meaning                                         |
| ---- | ----------------------------------------------- |
| `0`  | Clean — no forbidden imports                    |
| `1`  | One or more forbidden imports found             |
| `2`  | Usage / IO error (e.g. dist directory missing)  |

### Recommended workflow

```bash
npm run build && node scripts/import-audit.mjs
```
