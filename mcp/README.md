# amc-mcp

Thin Python MCP stdio wrapper that proxies four tools
(`list_unread_messages`, `send_message`, `mark_read`, `get_message_context`)
to the AMC adapter HTTP API.

This package is part of the AMC repo as a uv workspace member. Install all
deps from the repo root with `uv sync`; this package is then importable
and the `amc-mcp` console script lands on PATH inside the venv.

## Configuration (env vars)

| Variable            | Required | Default                  | Notes                          |
| ------------------- | -------- | ------------------------ | ------------------------------ |
| `AMC_BEARER_TOKEN`  | yes      | —                        | Adapter bearer token           |
| `AMC_AGENT_ID`      | yes      | —                        | Per-agent cursor identity      |
| `AMC_BASE_URL`      | no       | `http://127.0.0.1:8080`  | Adapter HTTP base URL          |

## Usage

Run the wrapper directly (it speaks MCP over stdio):

```bash
uv run --project mcp amc-mcp
```

Or wire it into an MCP host. Example Claude Desktop / Claude Code config:

```json
{
  "command": "uv",
  "args": ["--directory", "/absolute/path/to/amc/mcp", "run", "amc-mcp"],
  "env": {
    "AMC_BEARER_TOKEN": "…",
    "AMC_AGENT_ID": "claude-desktop"
  }
}
```

## Development

```bash
uv run --project mcp pytest                       # full suite
uv run --project mcp ruff check .                 # lint
uv run --project mcp ruff format --check .        # format check
uv run --project mcp python scripts/import_audit.py
```

## Scripts

### `scripts/import_audit.py`

Walks the `amc_mcp` package source and fails if any module imports a name
whose path contains a platform-specific token (`discord`, `applescript`,
`osascript`, `chat.db`, `imessage`). Spec §9.3 mandates "Wrapper has zero
platform-specific imports" — this is the static enforcement.

Unlike a substring grep, the audit parses each `.py` with `ast` and only
inspects module specifiers, so a tool description that mentions e.g.
"Discord" never produces a false positive.

Exit codes: `0` clean, `1` violations found, `2` usage / IO error.
