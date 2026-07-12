# claude_workspace

This directory is the working directory `claude_runner.py` hands to every
`claude -p` invocation. It exists so Claude auto-discovers the per-tool
permission allowlist in `.claude/settings.json`.

The MCP server config is **not** stored here as a `.mcp.json` file; it is
generated at runtime in `claude_runner.build_mcp_config_json()` and passed
to `claude` via `--mcp-config <inline-json>` together with
`--strict-mcp-config`. That avoids hard-coding an absolute path to the
sibling `mcp/` workspace member and keeps the bundled package contents
relocatable.

`--setting-sources project,local` (set by the runner) restricts setting
discovery to this directory, so user-global settings cannot leak in.
