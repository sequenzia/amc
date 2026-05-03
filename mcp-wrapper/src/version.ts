// Server identity. Hard-coded rather than read from package.json at runtime so
// that the compiled bundle has no JSON-import dependency (keeps the bundle
// portable across Node 20+ and Bun without --experimental flags).
//
// Keep this in sync with package.json `name` / `version`. A simple test in
// tests/version-sync.test.ts asserts the two stay aligned.

export const SERVER_NAME = 'amc-mcp-wrapper';
export const SERVER_VERSION = '0.1.0';
