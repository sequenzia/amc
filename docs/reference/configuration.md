# Configuration

AMC reads **all** of its configuration from environment variables, conventionally loaded from a single `.env` file. There is no YAML or command-line flag layer: if a setting exists, it is an `AMC_*` environment variable. This page is the canonical catalog of every variable the adapter, the MCP wrapper, and the webhook receiver understand, plus the format of the sender allowlist.

## Configuration files

Two files drive a running adapter, both under `~/.config/messaging-agent/`:

| File | Purpose | Override |
| --- | --- | --- |
| `~/.config/messaging-agent/.env` | Environment variables (secrets + tunables). Loaded once at startup with [python-dotenv](https://pypi.org/project/python-dotenv/). | (the path is fixed) |
| `~/.config/messaging-agent/allowlist.toml` | The sender allowlist — which iMessage handles and Discord user IDs are trusted. | `$AMC_ALLOWLIST_PATH` |

Both files hold secrets and personal names, so both should be owned by you and mode `0600`:

```bash
mkdir -p ~/.config/messaging-agent
chmod 0700 ~/.config/messaging-agent
touch ~/.config/messaging-agent/.env ~/.config/messaging-agent/allowlist.toml
chmod 0600 ~/.config/messaging-agent/.env ~/.config/messaging-agent/allowlist.toml
```

!!! warning "`.env` is not watched — restart after editing"
    The adapter loads `.env` exactly once, at process start. It does **not** watch the file for changes. After editing `.env`, restart the adapter:

    ```bash
    amc service restart adapter
    ```

    The **allowlist** is the exception: it can be hot-reloaded without a restart by sending `SIGHUP` to the adapter (see [The allowlist file](#the-allowlist-file) below). Changes to `.env` always require a restart.

## Adapter environment variables

These are read by the adapter process (`amc serve adapter`). Variables marked **required** cause the adapter to refuse to start when unset.

| Variable | Required? | Default | Purpose |
| --- | --- | --- | --- |
| `AMC_BEARER_TOKEN` | **Yes** | — | Shared secret presented as `Authorization: Bearer <token>` on every REST call. The adapter refuses to start without it. |
| `AMC_DB_PATH` | No | `~/Library/Application Support/messaging-agent/state.db` | SQLite database path. See the [state.db vs amc.db](#statedb-vs-amcdb) note below. |
| `AMC_ALLOWLIST_PATH` | No | `~/.config/messaging-agent/allowlist.toml` | Path to the sender allowlist TOML. |
| `AMC_ATTACHMENT_DIR` | No | `~/Library/Application Support/messaging-agent/attachments` | Directory where re-hosted attachment bytes are stored. |
| `AMC_ATTACHMENT_RETENTION_DAYS` | No | `90` | The attachment sweeper deletes re-hosted bytes older than this many days. Must be a positive integer. |
| `AMC_LOG_DIR` | No | `~/Library/Logs/messaging-agent` | Directory for daily-rotated structured (JSON) log files. |
| `AMC_RATE_LIMIT_PER_CHANNEL_RPS` | No | `1.0` | Per-channel outbound token-bucket refill rate (messages per second). Must be `> 0`. |
| `AMC_RATE_LIMIT_PER_CHANNEL_BURST` | No | `5` | Per-channel token-bucket capacity (max burst). Must be a positive integer. |
| `AMC_WEBHOOK_URL` | No | (empty) | Outbound webhook URL for new-message push. Empty/unset disables the webhook entirely. |
| `AMC_WEBHOOK_SECRET` | Conditional | — | HMAC-SHA256 shared secret for signing webhook payloads. **Required if `AMC_WEBHOOK_URL` is set** — the adapter fails to start when the URL is present but the secret is missing. |
| `AMC_DISCORD_BOT_TOKEN` | No | (empty) | Discord bot token. When set, the Discord connector starts; when unset, Discord is reported as `disabled`. |
| `AMC_BIND_HOST` | No | `127.0.0.1` | Host the adapter binds to. Also used to build attachment re-hosting URLs. |
| `AMC_BIND_PORT` | No | `8080` | Port the adapter binds to. Also used to build attachment re-hosting URLs. |

!!! danger "Keep your secrets out of version control"
    `AMC_BEARER_TOKEN`, `AMC_DISCORD_BOT_TOKEN`, and `AMC_WEBHOOK_SECRET` are credentials. Keep them in the mode-`0600` `.env` file, never in a committed file, a shell history line, or a process you launch with the value on the command line.

!!! tip "Generating a bearer token"
    Generate a strong, URL-safe token with:

    ```bash
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    ```

    Use the same value for `AMC_BEARER_TOKEN` in the adapter `.env` and in the MCP wrapper / receiver environment so both sides agree.

A minimal adapter `.env`:

```bash
# ~/.config/messaging-agent/.env  (chmod 0600)
AMC_BEARER_TOKEN=replace-with-a-generated-token

# Optional — enable Discord by supplying a bot token
AMC_DISCORD_BOT_TOKEN=replace-with-a-discord-bot-token

# Optional — enable outbound webhook push (secret becomes required)
# AMC_WEBHOOK_URL=http://127.0.0.1:8090/webhook
# AMC_WEBHOOK_SECRET=replace-with-a-second-generated-token
```

### state.db vs amc.db

!!! warning "Tracked spec ↔ code divergence: trust `state.db`"
    The code default for `AMC_DB_PATH` is `state.db` (`~/Library/Application Support/messaging-agent/state.db`). However, some operator docs and the spec (§11.2) refer to the database as `amc.db`. This is a known, tracked spec-versus-code divergence.

    **Trust `state.db` as the actual default** unless you explicitly set `AMC_DB_PATH` to something else. If you have followed older instructions and created an `amc.db`, either point `AMC_DB_PATH` at it or rename the file to match the code default.

    For the table layout the database holds, see the [Storage Schema](../architecture/storage.md).

## MCP wrapper environment variables

The MCP wrapper (`mcp/`) is a thin layer that translates MCP tool calls into adapter HTTP calls. It reads exactly three variables:

| Variable | Required? | Default | Purpose |
| --- | --- | --- | --- |
| `AMC_BEARER_TOKEN` | **Yes** | — | The **same** token value the adapter uses, presented as `Authorization: Bearer <token>`. |
| `AMC_AGENT_ID` | **Yes** | — | Identifies this wrapper instance. Sent as the `X-Agent-ID` header, which scopes per-agent unread cursors. Pick a stable name like `claude-code-laptop`. |
| `AMC_BASE_URL` | No | `http://127.0.0.1:8080` | Base URL of the adapter the wrapper talks to. Must be a valid URL. |

Because `AMC_AGENT_ID` scopes the unread cursor, two wrappers with different agent IDs maintain independent "what have I already seen" positions; two wrappers sharing an agent ID share one cursor. Keep the value stable across restarts so the cursor survives.

For how the wrapper is wired into an agent (MCP client config, environment passing), see the [Setup Guide](../getting-started/index.md).

## Webhook receiver environment variables

The webhook receiver is a **separate service** that bridges the adapter's outbound webhook to one-shot `claude -p` invocations. The variables below configure it; full operational detail lives on its [page](../operations/webhook-receiver.md).

| Variable | Required? | Default | Purpose |
| --- | --- | --- | --- |
| `AMC_WEBHOOK_SECRET` | **Yes** | — | HMAC-SHA256 secret. Must **match** the adapter's `AMC_WEBHOOK_SECRET` so signatures verify. |
| `AMC_BEARER_TOKEN` | **Yes** | — | Adapter bearer token; passed through to the MCP wrapper subprocess the receiver spawns. |
| `AMC_RECEIVER_BIND_HOST` | No | `127.0.0.1` | Host the receiver binds to. |
| `AMC_RECEIVER_BIND_PORT` | No | `8090` | Port the receiver binds to. |
| `AMC_AGENT_ID` | No | `amc-receiver` | Agent ID handed to the spawned MCP wrapper (sets `X-Agent-ID`). |
| `AMC_RECEIVER_AGENT_PROMPT_FILE` | No | `~/.config/messaging-agent/agent_prompt.md` | System/agent prompt file prepended to each `claude -p` invocation. |
| `AMC_RECEIVER_DANGEROUS` | No | `0` | Set to `1` to pass `--dangerously-skip-permissions` to `claude`. Leave at `0` unless you understand the implications. |
| `AMC_RECEIVER_CLAUDE_TIMEOUT_SECONDS` | No | `300` | Per-invocation timeout (seconds) for the `claude` subprocess. |
| `AMC_RECEIVER_LOG_DIR` | No | `~/Library/Logs/messaging-agent` | Directory for receiver log files. |
| `AMC_RECEIVER_IDLE_WORKER_TTL_SECONDS` | No | `300` | How long an idle per-channel worker lives before it is reaped. |
| `AMC_RECEIVER_DEDUPE_CACHE_SIZE` | No | `4096` | Size of the in-memory cache used to drop duplicate webhook deliveries. |
| `AMC_RECEIVER_CLAUDE_BIN` | No | `claude` | Name or path of the `claude` binary to invoke. |
| `AMC_BASE_URL` | No | `http://127.0.0.1:8080` | Adapter base URL handed to the spawned MCP wrapper. |

!!! danger "`AMC_RECEIVER_DANGEROUS=1` skips permission prompts"
    Setting `AMC_RECEIVER_DANGEROUS=1` passes `--dangerously-skip-permissions` to every `claude` invocation the receiver makes. That removes the per-action confirmation gate. Only enable it when you fully trust the prompt, the allowlist, and the tools the agent can reach.

## The allowlist file

The allowlist is the trust boundary for **inbound** messages. The adapter resolves every incoming message against `allowlist.toml`:

- A message from a sender **on** the allowlist is stored `allowlist_status='allowed'` and appears in `/messages/unread`.
- A message from a sender **not** on the allowlist is stored `allowlist_status='unknown'` and **never** appears in `/messages/unread`. Such messages are only visible via `/messages/quarantine`.

The file is a TOML document containing an array of `[[entry]]` tables — one entry per `(source, id)` pair:

| Field | Required? | Notes |
| --- | --- | --- |
| `source` | **Yes** | `"imessage"` or `"discord"`. Any other value is rejected. |
| `id` | **Yes** | Platform-native identifier. iMessage: an E.164 phone number or an email address. Discord: the numeric user ID **as a string**. |
| `display_name` | No | Overrides the platform-supplied display name. |
| `person_id` | No | Cross-platform identity tag. Entries sharing the same `person_id` are linked as one human in the `identity_links` table. |

A working example linking one iMessage handle and one Discord account to the same person:

```toml
# ~/.config/messaging-agent/allowlist.toml  (chmod 0600)

[[entry]]
source = "imessage"
id = "+15555550123"
display_name = "Ada Lovelace"
person_id = "ada"

[[entry]]
source = "discord"
id = "284731905603862528"
display_name = "Ada Lovelace"
person_id = "ada"
```

Because both entries above carry `person_id = "ada"`, the adapter materializes one `identity_links` row per entry under that person, so the agent treats the iMessage handle and the Discord account as the same human.

!!! warning "A missing or malformed allowlist is fatal"
    The adapter refuses to start if `allowlist.toml` is absent, is not valid TOML, contains an unknown `source`, is missing a required field, or has a duplicate `(source, id)` pair. Fix the file (and re-check the path / `$AMC_ALLOWLIST_PATH`) before starting.

!!! tip "Hot-reload with SIGHUP"
    Unlike `.env`, the allowlist can be reloaded without restarting the adapter. After editing the file, send `SIGHUP` to the adapter process:

    ```bash
    kill -HUP "$(pgrep -f 'amc serve adapter')"
    ```

    If the edited file fails validation, the adapter keeps the previously-loaded allowlist and logs the error rather than crashing.

For the end-to-end walkthrough of populating the allowlist and wiring up an agent, see the [Setup Guide](../getting-started/index.md).

## See also

- [Setup Guide](../getting-started/index.md) — full first-run walkthrough.
- [amc CLI](cli.md) — `amc doctor` validates your configuration and reports what is missing or misconfigured.
- [Runbook](../operations/runbook.md) — day-to-day operations and troubleshooting.
