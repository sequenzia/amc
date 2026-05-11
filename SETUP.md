# AMC Operator Setup Guide

This is the deployment runbook for the **Agent Messaging Channel (AMC)** — a single-Mac service that exposes one AI agent on iMessage and Discord behind a unified MCP + REST surface.

This guide takes you from a fresh checkout to a launchd-supervised adapter, an MCP wrapper wired into your agent host, and a verified inbound-and-outbound message round-trip.

It assumes you are the owner-operator of the host Mac and have admin rights to grant macOS privacy permissions. It is the v1 deployment path; spec §11.1 expects manual install on one Mac with no multi-host story.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone & Install](#2-clone--install)
3. [Environment Configuration](#3-environment-configuration)
4. [Sender Allowlist](#4-sender-allowlist)
5. [Discord Bot Setup](#5-discord-bot-setup)
6. [macOS Permissions for the iMessage Connector](#6-macos-permissions-for-the-imessage-connector)
7. [First Run (Foreground)](#7-first-run-foreground)
8. [launchd Supervision](#8-launchd-supervision)
9. [MCP Wrapper Installation](#9-mcp-wrapper-installation)
10. [End-to-End Verification](#10-end-to-end-verification)
11. [Updates & Rollback](#11-updates--rollback)
12. [Troubleshooting Pointers](#12-troubleshooting-pointers)

---

## 1. Prerequisites

| Requirement | Why | How to check |
|-------------|-----|--------------|
| **macOS** (Apple Silicon or Intel) | The iMessage connector reads `~/Library/Messages/chat.db` and drives Messages.app via AppleScript — both macOS-only. | `sw_vers` |
| **Python 3.12 or newer** | Adapter uses stdlib `tomllib` and 3.12-only typing features. The repo pins to 3.12 via `.python-version`; `uv` will fetch a matching interpreter for you. | `python3 --version` |
| **`uv`** (Python package manager) | The repo standardises on `uv` for dependency resolution and execution. The MCP wrapper is a Python uv workspace member at `mcp/` — no Node toolchain required. | `which uv` |
| **A working Apple ID signed into Messages.app** | iMessage send and receive requires an active iMessage account on the host. | Open Messages.app → Settings → iMessage account is enabled. |
| **A Discord account** with permission to create an Application | You will register a bot user and invite it to one server. | Log into <https://discord.com/developers>. |

Install the missing pieces (Homebrew is the easy path):

```bash
brew install uv
```

The adapter does **not** require Docker, a database server, or any other Mac to be reachable. SQLite is on-disk; everything binds to `127.0.0.1:8080` by default.

---

## 2. Clone & Install

```bash
# Pick a stable install location — launchd will reference this path.
mkdir -p ~/code && cd ~/code
git clone <repo-url> amc
cd amc

# Install adapter + MCP wrapper deps into a uv-managed venv (.venv at repo root).
# The wrapper is a uv workspace member at `mcp/`; --all-packages installs both.
uv sync --all-packages
```

`uv sync` reads the root `pyproject.toml` + `uv.lock` (and `mcp/pyproject.toml`) and produces `.venv/`. After this you can run any Python entry point with `uv run <cmd>`, and the wrapper's `amc-mcp` console script is on PATH inside the venv.

Verify both layers built:

```bash
uv run python -c "import amc; print(amc.__name__)"        # → amc
uv run python -c "import amc_mcp; print(amc_mcp.__name__)"  # → amc_mcp
```

> **Install location matters.** `amc install` burns the absolute path of the repo into `~/Library/LaunchAgents/com.user.amc-adapter.plist` (and the other service plists). If you move the repo later, re-run `amc install` to re-render.

---

## 3. Environment Configuration

The adapter reads its configuration from a single file: **`~/.config/messaging-agent/.env`**. This path is hard-coded in `amc.core.auth.load_bearer_token()` and used by every module that needs an `AMC_*` variable.

### 3.1 Create the file

```bash
mkdir -p ~/.config/messaging-agent
cp .env.example ~/.config/messaging-agent/.env
chmod 0600 ~/.config/messaging-agent/.env
```

The `0600` mode is non-negotiable — the file holds your bearer token, Discord bot token, and webhook HMAC secret. Anyone with read access to the file can impersonate the agent against your adapter.

### 3.2 Generate a bearer token

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the result into `AMC_BEARER_TOKEN=` in the `.env` file.

### 3.3 Required and optional variables

The file shipped as `.env.example` lists every variable the adapter understands (spec §11.2). The minimum set you must populate for a Discord-only deployment:

| Variable | Required? | Notes |
|----------|-----------|-------|
| `AMC_BEARER_TOKEN` | **Yes** | Generated above. Every REST call carries `Authorization: Bearer <token>`. |
| `AMC_DISCORD_BOT_TOKEN` | **Yes** if Discord is enabled | From the Developer Portal (§5 below). Treat as a credential, not a config value. |
| `AMC_WEBHOOK_URL` | Optional | Empty disables the outbound webhook. Set to the agent's listener URL if you want push notifications instead of polling `/messages/unread`. |
| `AMC_WEBHOOK_SECRET` | **Yes** if `AMC_WEBHOOK_URL` is set | HMAC-SHA256 shared secret signed into every webhook delivery so the receiver can verify authenticity. |
| `AMC_BIND_HOST` / `AMC_BIND_PORT` | No | Defaults `127.0.0.1:8080`. Leave loopback unless you're fronting the adapter with a tunnel or reverse proxy. |
| `AMC_DB_PATH`, `AMC_ATTACHMENT_DIR`, `AMC_LOG_DIR`, `AMC_ALLOWLIST_PATH` | No | Defaults under `~/Library/Application Support/messaging-agent/` and `~/Library/Logs/messaging-agent/`. Override only if you have a strong reason — the launchd plist and most tooling assume the defaults. |
| `AMC_RATE_LIMIT_PER_CHANNEL_RPS` (`1`) and `AMC_RATE_LIMIT_PER_CHANNEL_BURST` (`5`) | No | Per-channel token bucket. Keep low; raising these without coordination will trip platform-side limits. |
| `AMC_ATTACHMENT_RETENTION_DAYS` (`90`) | No | The attachment sweeper deletes re-hosted blobs older than this many days. |

> **Where the `.env` file is loaded.** The adapter reads it at startup via `amc.core.auth.load_bearer_token()`. It is **not** auto-watched — change the file and restart the adapter (`amc service restart adapter`) for new values to take effect.

---

## 4. Sender Allowlist

Inbound messages from senders not on the allowlist are stored with `allowlist_status='unknown'` and **never** appear in `/messages/unread` (they are reachable only via `/messages/quarantine`). Spec §5.1 + §5.7 — this is the v1 trust boundary, not a "nice to have".

Create the file:

```bash
mkdir -p ~/.config/messaging-agent
$EDITOR ~/.config/messaging-agent/allowlist.toml
```

Minimal, working example:

```toml
# ~/.config/messaging-agent/allowlist.toml
# One [[entry]] block per (source, id) pair.
# `source` is "imessage" or "discord".
# `id` is the platform-native sender ID:
#   - imessage: E.164 phone (+15551234567) or email (someone@example.com)
#   - discord:  numeric user ID as a string (e.g. "123456789012345678")
# `display_name` is optional and overrides the platform-supplied name.
# `person_id` is optional; entries sharing the same person_id are linked
# in the identity_links table so the agent sees them as one human.

[[entry]]
source = "imessage"
id = "+15551234567"
display_name = "Alex Owner"
person_id = "alex"

[[entry]]
source = "discord"
id = "123456789012345678"
display_name = "Alex Owner"
person_id = "alex"

[[entry]]
source = "imessage"
id = "friend@example.com"
display_name = "Trusted Friend"
person_id = "friend"
```

Restrict permissions (the file may name people you'd rather not advertise):

```bash
chmod 0600 ~/.config/messaging-agent/allowlist.toml
```

> **The adapter refuses to start with a missing or malformed allowlist.** Spec §5.1 error handling. If you change the file while the adapter is running, send `SIGHUP` to reload in place (`kill -HUP "$(amc status adapter --json | jq -r '.pid')"`) — the adapter does not need a restart for allowlist edits.

---

## 5. Discord Bot Setup

The Discord connector authenticates as a **bot user**, not as your personal account. You create the bot in the Developer Portal, copy its token into `~/.config/messaging-agent/.env`, and invite it to whichever server(s) you want it to read.

### 5.1 Create the application and bot

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it whatever you'd like — only you will see it.
2. Open the new application → **Bot** sidebar → **Add Bot** → confirm.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**. Without this the gateway delivers `MESSAGE_CREATE` events with empty `content` and the connector cannot do anything useful. (Spec §5.1 calls this out explicitly.)
4. Optional but recommended: disable **Public Bot** so nobody else can invite your bot to their servers.
5. Click **Reset Token**, copy the token immediately, and paste it into `AMC_DISCORD_BOT_TOKEN=` in `~/.config/messaging-agent/.env`. The portal will not show this value again.

### 5.2 Invite the bot to your server

1. Open the application → **OAuth2 → URL Generator**.
2. Scopes: tick **`bot`**.
3. Bot Permissions: tick **Read Messages/View Channels**, **Send Messages**, and **Read Message History**. Add **Attach Files** if you want the agent to send attachments.
4. Copy the generated URL, open it in a browser logged into the destination server's admin account, and authorize.
5. Confirm the bot appears in the server's member list. It will show as offline until the adapter connects to the gateway.

### 5.3 Note the user IDs you want to allowlist

In Discord: **User Settings → Advanced → Developer Mode → On**. Then right-click any user and **Copy User ID**. Paste these into `allowlist.toml` under `source = "discord"` entries (§4 above).

---

## 6. macOS Permissions for the iMessage Connector

The adapter runs as a normal user-space process on macOS, but the iMessage path crosses two protected boundaries that require explicit operator action: reading the Messages database and driving Messages.app over AppleScript. Both prompts are one-time, but they will not appear until the adapter actually attempts the operation, and the adapter will silently fail until they are accepted. Grant these before relying on the connector in production.

### 6.1 Full Disk Access (FDA)

**Why:** The iMessage connector polls `~/Library/Messages/chat.db` for new messages. macOS protects this path under TCC (Transparency, Consent, and Control); without Full Disk Access, `sqlite3.connect(...)` returns "operation not permitted" even though the file is owned by the running user.

**How:**

1. Open **System Settings → Privacy & Security → Full Disk Access**.
2. Click the **+** button and add the binary that runs the adapter:
   - If launching via `uv run`, add the resolved interpreter (run `uv run python -c "import sys; print(sys.executable)"` to find it) **and** `/usr/bin/uv` if `uv` itself spawns the child.
   - If running under a process supervisor (launchd, tmux, iTerm2), add the supervising binary as well — TCC checks the parent process, not just the child.
   - For a `caffeinate`-wrapped invocation, add `/usr/bin/caffeinate`.
3. Toggle the entry **on**.
4. Fully quit and restart the adapter process. TCC permission is read at process start; an already-running process will not pick it up.

**Verify:**

```bash
amc logs adapter
```

Look for a structured log line of the form:

```json
{"event": "connector_state", "component": "imessage_connector", "state": "ok"}
```

If you see `state=error` with `errno=1` or a message containing `operation not permitted`, FDA was not granted to the right binary — repeat step 2 with the parent process included.

### 6.2 Automation (Messages.app control)

**Why:** Outbound sends use AppleScript to drive Messages.app (`tell application "Messages" to send ...`). macOS gates cross-application scripting behind the **Automation** TCC bucket, which is separate from FDA. The first send attempt triggers a system prompt; until it is accepted the AppleScript call returns success but Messages.app silently drops the request.

**How:**

1. Trigger the prompt manually with a no-op script so the operator is at the keyboard when it appears (rather than discovering it later from a failed agent send):

   ```bash
   osascript -e 'tell application "Messages" to get name'
   ```

2. macOS displays:

   > "Terminal" (or your shell host) wants to control "Messages". Allowing control will provide access to documents and data in "Messages", and to perform actions within that app.

   Click **OK**.

3. If you missed the prompt or clicked Don't Allow, re-enable manually under **System Settings → Privacy & Security → Automation**, expand the controlling app (e.g. Terminal, iTerm, the Python interpreter, `uv`), and tick the **Messages** checkbox.

4. Repeat the prompt-trigger from whatever process actually owns the adapter. The Automation grant is per **(controller, target)** pair: granting it to Terminal does not grant it to `uv` or to a launchd-spawned Python.

**Verify:** Send a test message through the adapter:

```bash
curl -X POST http://127.0.0.1:8080/messages/send \
  -H "Authorization: Bearer $AMC_BEARER_TOKEN" \
  -H "X-Agent-ID: setup-check" \
  -H "Content-Type: application/json" \
  -d '{"channel_id":"imsg:+15551234567","text":"setup check"}'
```

Expected: HTTP 200 with a normalized envelope echo, and the message visibly appears in Messages.app on the host Mac. If the API returns 200 but the message never appears, Automation was not granted to the adapter's actual parent process.

### 6.3 Prevent Automatic Sleep

**Why:** The iMessage poller runs as a normal asyncio task. When the Mac sleeps, the process is suspended — the polling loop pauses, `chat.db` is not read, and inbound messages are not surfaced to the agent until the machine wakes. Display sleep alone is fine; system / disk sleep is the problem.

**How — pick one:**

- **Foreground wrapper** (simplest, recommended for development):

  ```bash
  caffeinate -dimsu -- amc serve adapter
  ```

  Flags: `-d` prevent display sleep, `-i` prevent idle sleep, `-m` prevent disk sleep, `-s` prevent system sleep on AC power, `-u` declare user activity. The `--` separates `caffeinate` flags from the wrapped command.

- **Persistent setting** (for a dedicated host):

  Open **System Settings → Battery → Options** (laptops) or **System Settings → Energy** (desktops/Mac mini) and set:
  - "Prevent automatic sleeping when the display is off" → **On**
  - "Wake for network access" → **On** (helps the Discord WebSocket reconnect cleanly after lid-close events)

- **launchd-managed adapter:** wrap the `ProgramArguments` in `caffeinate -dimsu` so the keep-awake assertion is tied to the adapter's lifetime, not a login shell.

**Verify:**

```bash
pmset -g assertions | grep -E "PreventUserIdleSystemSleep|PreventSystemSleep"
```

You should see at least one assertion held by `caffeinate` (or by the adapter itself if it asserts directly). If the list is empty and you are relying on Energy settings, double-check that "Put hard disks to sleep when possible" is **off** as well.

### 6.4 Permission Recovery Checklist

If the iMessage connector silently stops working after an OS update, system migration, or reinstall, walk this list before debugging code:

1. Did the adapter binary path change (e.g. new `uv` cache, new Python interpreter)? Re-add it to **Full Disk Access** — TCC tracks by binary path + code signature, not by name.
2. Was Messages.app updated? The Automation grant is preserved across updates, but a Messages.app reinstall resets it.
3. Did macOS reset TCC? Run `tccutil reset SystemPolicyAllFiles` and `tccutil reset AppleEvents` only as a last resort — this clears every app's grants and forces re-prompting across the system.
4. Is the Mac actually awake when polling should be happening? `pmset -g log | grep -i sleep` shows recent sleep/wake events.

---

## 7. First Run (Foreground)

Run the adapter in the foreground first so any startup errors land on your terminal, not in a launchd log file. Once you've confirmed it's healthy, install the LaunchAgent (§8).

### 7.1 Apply database migrations

```bash
uv run alembic upgrade head
```

This creates `~/Library/Application Support/messaging-agent/amc.db` (or wherever `AMC_DB_PATH` points) with the spec §7.3 schema: `messages`, `channels`, `senders`, `identity_links`, plus connector state and webhook delivery tables. Alembic prints `Running upgrade` lines for each migration; the final line is `INFO  [alembic.runtime.migration] Will assume non-transactional DDL.` followed by silence.

To inspect afterwards:

```bash
sqlite3 ~/Library/Application\ Support/messaging-agent/amc.db '.tables'
```

### 7.2 Start the adapter

Run the adapter in the foreground via the CLI (`amc serve adapter` execs
`uv run uvicorn amc.app:app --host 127.0.0.1 --port 8080` in-place):

```bash
amc serve adapter
```

You should see structured log lines starting with `event=startup` and (within ~2 seconds) `event=connector_state component=discord_connector state=ok`. The iMessage connector logs `state=ok` only after FDA has been granted to the actual interpreter path (see §6.1).

### 7.3 Verify `/healthz`

In a second terminal:

```bash
export AMC_BEARER_TOKEN="$(grep ^AMC_BEARER_TOKEN ~/.config/messaging-agent/.env | cut -d= -f2-)"
curl -sS -H "Authorization: Bearer $AMC_BEARER_TOKEN" http://127.0.0.1:8080/healthz | python3 -m json.tool
```

Expected response (HTTP 200):

```json
{
    "status": "ok",
    "uptime_seconds": 12,
    "connectors": {
        "imessage": { "state": "ok",       "last_message_at": null },
        "discord":  { "state": "ok",       "last_message_at": null }
    },
    "webhook_queue": { "pending": 0, "dead": 0 },
    "version": "0.1.0"
}
```

If `connectors.imessage.state` is `"degraded"`, the iMessage connector started but cannot read `chat.db` — return to §6.1. If `connectors.discord.state` is `"degraded"`, the bot token is rejected or the Message Content intent is not enabled — return to §5.

If `/healthz` returns `401 Unauthorized`, the bearer token in your env doesn't match the one in `~/.config/messaging-agent/.env`. The adapter only reads the file at startup; if you edited it after launching, restart the adapter.

Stop the foreground adapter with `Ctrl-C` once `/healthz` returns 200.

---

## 8. launchd Supervision

For the adapter to survive logout, system sleep/wake, and crashes, install it as a per-user LaunchAgent. The plist templates live in `ops/launchd/`; the `amc` CLI is the install / lifecycle front end.

### 8.1 Install

```bash
amc install
```

`amc install` (with no service name) installs every known service —
`adapter`, `receiver`, and `backup`. To install only specific services:

```bash
amc install adapter           # adapter only
amc install adapter receiver  # adapter + webhook receiver
```

Internally, `amc install` for each named service:

1. Ensures `~/Library/Logs/messaging-agent/` exists (launchd will not create parent directories for `StandardOutPath`).
2. Renders the template plist from `ops/launchd/` with your install dir and `$HOME` substituted in.
3. Validates the rendered plist with `plutil -lint`.
4. Atomically writes it to `~/Library/LaunchAgents/<label>.plist`.
5. Bootstraps the service into the GUI user domain.
6. Marks it enabled.

Re-running is safe — `amc install` unloads any existing copy first.

Run the bundled health-check next to confirm the install is sound:

```bash
amc doctor
```

`amc doctor` walks FDA, Automation, `.env` presence + mode, allowlist
presence, plist install state, service state, and `/healthz`.

### 8.2 Verify

```bash
amc status adapter
```

Look for `state = running` and a non-zero `pid`. Follow the launchd-level
logs (process spawn output, not adapter structured logs):

```bash
amc logs adapter --launchd
```

Re-run `curl /healthz` from §7.3 to confirm the launchd-spawned process is serving traffic.

### 8.3 Restart policy

The plist sets:

- `RunAtLoad=true` — start at login / boot.
- `KeepAlive={SuccessfulExit:false}` — restart on crash, but not on a clean `exit 0`.
- `ThrottleInterval=10` — minimum 10 s between respawns to avoid busy-spinning a crash loop.

This satisfies the spec §6.4 RTO target of ≤ 5 minutes after a process crash.

### 8.4 Restart, uninstall

```bash
# Restart in place (after a config change in ~/.config/messaging-agent/.env):
amc service restart adapter

# Stop / start without removing the install:
amc service stop adapter
amc service start adapter

# Uninstall (bootout + remove plist):
amc uninstall adapter

# Uninstall but keep the plist on disk for inspection:
amc uninstall adapter --keep-plist
```

> **Configuration belongs in `.env`, not the plist.** The `EnvironmentVariables` dict in the plist is intentionally empty. The adapter loads `~/.config/messaging-agent/.env` at startup, so a config change does not require reinstalling the LaunchAgent — `amc service restart adapter` is enough.

---

## 9. MCP Wrapper Installation

The MCP wrapper is a thin Python stdio process (built on the official `mcp` SDK / FastMCP) that proxies four MCP tools (`list_unread_messages`, `send_message`, `mark_read`, `get_message_context`) into HTTP calls against the running adapter. It contains no platform-specific code; the adapter is still the source of truth.

### 9.1 Required environment variables

The wrapper reads three env vars at startup (`mcp/src/amc_mcp/config.py`):

| Variable | Required? | Notes |
|----------|-----------|-------|
| `AMC_BEARER_TOKEN` | **Yes** | Same value as in `~/.config/messaging-agent/.env`. |
| `AMC_AGENT_ID` | **Yes** | Identifies this wrapper instance to the adapter. The adapter uses it as the `X-Agent-ID` header on every call, which scopes per-agent cursors (`/messages/unread` is per-agent in v1). Pick a stable, human-readable string per agent host: `claude-code-laptop`, `codex-cli`, `claude-desktop-default`. |
| `AMC_BASE_URL` | No | Default `http://127.0.0.1:8080`. Override only if the adapter is on a different bind / port. |

### 9.2 Wire into your MCP host

The wrapper is invoked as `uv --directory /absolute/path/to/amc/mcp run amc-mcp` with the env vars above. (`uv` activates the workspace's `.venv` so the `amc-mcp` console script resolves without modifying the host's PATH.) Concrete examples follow; substitute your absolute install path.

#### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` and add an entry under `mcpServers`:

```json
{
  "mcpServers": {
    "amc": {
      "command": "uv",
      "args": ["--directory", "/Users/you/code/amc/mcp", "run", "amc-mcp"],
      "env": {
        "AMC_BEARER_TOKEN": "<paste token from ~/.config/messaging-agent/.env>",
        "AMC_AGENT_ID": "claude-desktop-default",
        "AMC_BASE_URL": "http://127.0.0.1:8080"
      }
    }
  }
}
```

Restart Claude Desktop. The four AMC tools should appear in the tool palette.

#### Claude Code

Add a server entry to `~/.config/claude-code/mcp.json` (create the file if it doesn't exist):

```json
{
  "mcpServers": {
    "amc": {
      "command": "uv",
      "args": ["--directory", "/Users/you/code/amc/mcp", "run", "amc-mcp"],
      "env": {
        "AMC_BEARER_TOKEN": "<token>",
        "AMC_AGENT_ID": "claude-code-laptop"
      }
    }
  }
}
```

Or run `claude mcp add amc uv -- --directory /Users/you/code/amc/mcp run amc-mcp` and then `claude mcp env amc set AMC_BEARER_TOKEN=... AMC_AGENT_ID=claude-code-laptop`.

#### Codex CLI

Edit `~/.codex/config.toml`:

```toml
[mcp_servers.amc]
command = "uv"
args = ["--directory", "/Users/you/code/amc/mcp", "run", "amc-mcp"]

[mcp_servers.amc.env]
AMC_BEARER_TOKEN = "<token>"
AMC_AGENT_ID = "codex-cli"
```

#### Direct binary invocation

If your MCP host doesn't tolerate `uv` as the spawn command, point it at the absolute venv path instead: `command = "/Users/you/code/amc/.venv/bin/amc-mcp"`. The console script imports `amc_mcp` from the venv and works identically. The `uv` form is preferred because it survives `uv sync --reinstall`.

### 9.3 Smoke-test the wrapper outside an MCP host

Before debugging in your MCP host, confirm the wrapper itself works. Pipe an `initialize` JSON-RPC line through stdin and confirm the response:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}\n' \
  | AMC_BEARER_TOKEN=$(grep ^AMC_BEARER_TOKEN ~/.config/messaging-agent/.env | cut -d= -f2-) \
    AMC_AGENT_ID=smoke-test \
    uv --directory /Users/you/code/amc/mcp run amc-mcp \
  | head -1
```

You should see a single JSON line containing `"serverInfo":{"name":"amc-mcp","version":"0.1.0"}`. If it instead prints `WrapperConfigError: Missing required env var(s): ...`, your env vars aren't reaching the process.

For an interactive end-to-end check, the official Anthropic Inspector still works (it spawns any MCP server and gives you a browser UI to click through tools):

```bash
npx @modelcontextprotocol/inspector uv --directory /Users/you/code/amc/mcp run amc-mcp
```

---

## 10. End-to-End Verification

A full deployment is verified by an inbound message arriving in `/messages/unread` and an outbound reply landing on the platform.

Set a shell variable so the curl examples below are copy-pasteable:

```bash
export AMC_BEARER_TOKEN="$(grep ^AMC_BEARER_TOKEN ~/.config/messaging-agent/.env | cut -d= -f2-)"
```

### 10.1 Inbound (Discord)

1. From a Discord account that is on the allowlist (§4), send any DM to your bot or post in a channel where the bot is present.
2. Within ~3 s, the message should be visible:

   ```bash
   curl -sf \
     -H "Authorization: Bearer $AMC_BEARER_TOKEN" \
     -H "X-Agent-ID: verify" \
     "http://127.0.0.1:8080/messages/unread?source=discord&limit=5" \
     | python3 -m json.tool
   ```

   Expected: a JSON array with one or more envelopes containing `"source": "discord"` and your message text.

3. If the array is empty: confirm `/healthz` shows `connectors.discord.state == "ok"`, confirm the sender's user ID is in `allowlist.toml` with `source = "discord"`, and check `~/Library/Logs/messaging-agent/adapter-$(date +%Y-%m-%d).log` for `event=allowlist_reject`.

### 10.2 Inbound (iMessage)

1. From an allowlisted iMessage handle, send any message to the host Mac.
2. Re-run the `/messages/unread` query above with `?source=imessage`. The message should appear within ~3 s.

If it doesn't, the most likely causes (in order):

- FDA not granted to the actual adapter binary (§6.1).
- Mac is asleep — confirm `pmset -g assertions` shows a `PreventSystemSleep` assertion (§6.3).
- Sender handle isn't in the allowlist with the correct E.164 format.

### 10.3 Outbound (reply)

Pick a `message_id` from the `/messages/unread` response and reply. The simplest manual test:

```bash
curl -X POST http://127.0.0.1:8080/messages/send \
  -H "Authorization: Bearer $AMC_BEARER_TOKEN" \
  -H "X-Agent-ID: verify" \
  -H "Content-Type: application/json" \
  -d '{"channel_id":"<channel_id from envelope>","text":"hello from amc"}'
```

Expected: HTTP 200 with `{ "message_id": "msg_...", "sent_at": "..." }` and the message visible in the platform client (Discord channel or Messages.app conversation).

### 10.4 Mark read

Once the agent has processed a message, it must mark it read so it doesn't reappear in `/messages/unread`:

```bash
curl -X POST http://127.0.0.1:8080/messages/mark_read \
  -H "Authorization: Bearer $AMC_BEARER_TOKEN" \
  -H "X-Agent-ID: verify" \
  -H "Content-Type: application/json" \
  -d '{"message_ids":["msg_..."]}'
```

The unread cursor is per-`X-Agent-ID`. Marking read with `verify` does not affect what your real `claude-code-laptop` agent sees — handy for setup-time poking that won't disturb a running agent.

### 10.5 Real agent round-trip

Open your MCP host (e.g. Claude Desktop) and ask the agent something like *"What unread messages do I have?"* The agent should call `list_unread_messages`, see the test message you sent in §10.1, and reply via `send_message`. That confirms the full path: platform → connector → adapter → MCP wrapper → agent → MCP wrapper → adapter → connector → platform.

---

## 11. Updates & Rollback

Per spec §11.1, the update procedure is:

```bash
cd ~/code/amc        # or wherever you installed
git pull
uv sync --all-packages
uv run alembic upgrade head
amc service restart adapter
```

Re-run `amc install` only if a plist template under `ops/launchd/` itself changed in the pull (`amc install` is idempotent).

**Rollback:**

```bash
uv run alembic downgrade -1   # if a migration is at fault
git checkout <previous-sha>
uv sync --all-packages
amc service restart adapter
```

---

## 12. Troubleshooting Pointers

The full runbook lives in `RUNBOOK.md` (post-handoff). Quick-reference symptoms and where to look first:

| Symptom | First place to check |
|---------|----------------------|
| `/healthz` returns 401 | Bearer token in shell env vs. file; restart adapter after editing `.env`. |
| `connectors.discord.state == "degraded"` | Bot token in `.env`; **Message Content Intent** enabled in Developer Portal; `~/Library/Logs/messaging-agent/adapter-*.log` for `event=connector_state`. |
| `connectors.imessage.state == "degraded"` | FDA grant on the actual adapter interpreter (§6.1); `chat.db` accessible (`sqlite3 ~/Library/Messages/chat.db .tables`). |
| Send returns 200 but message never arrives on iMessage | Automation grant on the adapter's parent process (§6.2). |
| Inbound iMessage stops after a long gap | Mac slept (§6.3); `pmset -g log \| grep -i sleep`. |
| Webhooks all dead-lettered | Webhook URL reachable from the host; HMAC secret matches receiver; check `webhook_deliveries WHERE status='dead'`. |
| Adapter won't start under launchd | `amc doctor`, then `amc logs adapter --launchd --no-follow`; usually a missing `.env`, missing `AMC_BEARER_TOKEN`, or absent allowlist file. |
| MCP wrapper exits immediately | Missing `AMC_BEARER_TOKEN` or `AMC_AGENT_ID` in the host's `env` block; the wrapper logs `WrapperConfigError` to stderr. |

For anything not covered here, the structured JSON logs at `~/Library/Logs/messaging-agent/adapter-YYYY-MM-DD.log` are the source of truth — every component logs `event=` and `component=` fields you can grep on.
