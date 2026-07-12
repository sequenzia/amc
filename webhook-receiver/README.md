# amg-webhook-receiver

Bridges the AMG adapter's outbound webhook to a one-shot `claude -p`
invocation per inbound message. The agent wakes only when there is a real
inbound message in the queue, decides whether and how to reply via the
AMG MCP tools, and exits.

This package is a uv workspace member of the AMG repo. Install all deps
from the repo root with `uv sync --all-packages`; the `amg-webhook-receiver`
console script then lands on PATH inside the venv.

## How it works

1. The AMG adapter receives an inbound message, persists it, and POSTs the
   normalized envelope to `AMG_WEBHOOK_URL` (HMAC-signed with
   `AMG_WEBHOOK_SECRET`).
2. This service verifies the signature, dedupes by `X-AMG-Delivery-Id`,
   and enqueues the envelope to a per-channel asyncio worker.
3. The worker spawns `claude -p` with:
   - `--mcp-config <inline-json>` pointing at the AMG MCP wrapper (same
     venv, invoked as `<sys.executable> -m amg_mcp`).
   - `--strict-mcp-config` so user-global `~/.claude.json` MCP servers do
     not leak in.
   - `--setting-sources project,local` so user-global settings (and
     permissions) do not leak in.
   - `--allowedTools mcp__amg__list_unread_messages mcp__amg__send_message
     mcp__amg__mark_read mcp__amg__get_message_context` (or
     `--dangerously-skip-permissions` if `AMG_RECEIVER_DANGEROUS=1`).
   - The system prompt loaded from `AMG_RECEIVER_AGENT_PROMPT_FILE`
     (default `~/.config/messaging-agent/agent_prompt.md`); falls back to
     the bundled `prompts/default_agent_prompt.md`.
   - The envelope JSON piped to stdin.
   - A scrubbed environment: `ENABLE_TOOL_SEARCH=false` is forced and the
     outer Claude Code session vars (`CLAUDECODE`, `CLAUDE_CODE_*`) are
     stripped (see "Nested-agent environment hygiene" below).
4. The webhook is ACK'd with `204` as soon as the envelope is enqueued
   (Claude can take many seconds; the adapter's HTTP timeout is 10s).
5. Errors during processing are logged but do not surface back to the
   adapter — those are not transport errors and we already accepted.

### Nested-agent environment hygiene

`ClaudeRunner._build_env()` does **not** pass the raw parent environment to
the nested `claude -p`. Two adjustments are critical:

- **`ENABLE_TOOL_SEARCH=false`** (forced, overridable). Claude Code 2.1.x
  defaults to *MCP tool search*: with it on, the four `mcp__amg__*` tools are
  **deferred** behind the `ToolSearch` tool and the `amg` server reports
  `status: pending` at session init. A headless one-shot agent then
  intermittently concludes "the MCP server hasn't finished connecting" and
  ends the turn **without ever loading or calling the tools** — so no reply is
  sent. Forcing it off loads the four tools directly at init and makes Claude
  block on a still-connecting server (`WaitForMcpServers`) before its first
  tool call, which is deterministic. Set your own `ENABLE_TOOL_SEARCH` to
  override. (Ref: <https://code.claude.com/docs/en/mcp.md#configure-tool-search>.)
- **Scrub `CLAUDECODE` / `CLAUDE_CODE_*`.** When the receiver is launched from
  inside an interactive Claude Code session (rather than under launchd), these
  session vars leak in and would make the nested `claude` behave as part of the
  outer session (shared session id, task list, tmpdir). Under launchd they are
  absent, so the scrub is a no-op there.

Concurrency: messages on the **same** `channel_id` process strictly in
arrival order (one Claude invocation at a time per chat). Different
channels run in parallel.

## Configuration (env vars)

| Variable                                  | Required | Default | Notes                             |
|-------------------------------------------|----------|---------|-----------------------------------|
| `AMG_WEBHOOK_SECRET`                      | yes      | —       | Shared HMAC secret with the adapter (the same env var the adapter uses). |
| `AMG_BEARER_TOKEN`                        | yes      | —       | Adapter bearer token (passed through to the MCP wrapper). |
| `AMG_RECEIVER_BIND_HOST`                  | no       | `127.0.0.1` | Bind interface. |
| `AMG_RECEIVER_BIND_PORT`                  | no       | `8090`  | Bind port. |
| `AMG_AGENT_ID`                            | no       | `amg-receiver` | Per-agent cursor identity for the MCP wrapper. |
| `AMG_RECEIVER_AGENT_PROMPT_FILE`          | no       | `~/.config/messaging-agent/agent_prompt.md` | System prompt source. |
| `AMG_RECEIVER_DANGEROUS`                  | no       | `0`     | `1` → pass `--dangerously-skip-permissions`. |
| `AMG_RECEIVER_CLAUDE_TIMEOUT_SECONDS`     | no       | `300`   | Per-message wall clock. |
| `AMG_RECEIVER_LOG_DIR`                    | no       | `~/Library/Logs/messaging-agent` | Reuses adapter directory. |
| `AMG_RECEIVER_IDLE_WORKER_TTL_SECONDS`    | no       | `300`   | Per-channel worker idle eviction. |
| `AMG_RECEIVER_DEDUPE_CACHE_SIZE`          | no       | `4096`  | LRU size for delivery-id dedupe. |
| `AMG_RECEIVER_CLAUDE_BIN`                 | no       | `claude`| Path to the `claude` binary; useful for tests. |
| `AMG_BASE_URL`                            | no       | `http://127.0.0.1:8080` | Adapter HTTP base for the MCP wrapper. |

## Adapter side — point the webhook at the receiver

Add to `~/.config/messaging-agent/.env`:

```
AMG_WEBHOOK_URL=http://127.0.0.1:8090/webhook
AMG_WEBHOOK_SECRET=<openssl rand -hex 32>
```

The adapter restarts pick this up automatically; no code change needed.

## Local end-to-end

```bash
# Terminal 1 — adapter
uv run uvicorn amg.app:app --host 127.0.0.1 --port 8080

# Terminal 2 — receiver
uv run --project webhook-receiver \
    uvicorn amg_receiver.app:app --host 127.0.0.1 --port 8090

# Terminal 3 — send yourself an iMessage / Discord DM, then watch:
tail -f ~/Library/Logs/messaging-agent/receiver-*.log
```

## Production install (launchd)

```bash
amg install    # installs adapter, receiver, and backup services
```

See `ops/launchd/README.md` for details.

## Development

```bash
uv run --project webhook-receiver pytest                  # full suite
uv run --project webhook-receiver ruff check .            # lint
uv run --project webhook-receiver ruff format --check .   # format check
```

## Customizing the agent persona

Drop a markdown file at `~/.config/messaging-agent/agent_prompt.md`. The
receiver re-reads it on every invocation, so prompt edits take effect
without restarting the service.
