# `amc` CLI

The `amc` command is the operator-facing CLI that ships with the AMC adapter (built with [Typer](https://typer.tiangolo.com/) + [Rich](https://rich.readthedocs.io/), package `amc/cli/`). It is everything you need to install, inspect, and troubleshoot AMC on a single Mac: it wraps the foreground service entry points, the launchd plist render/install pipeline, and preflight diagnostics.

After `uv sync --all-packages`, the `amc` entry point is on your `PATH`.

```bash
uv sync --all-packages
amc --help
amc --version
```

## Overview

`amc` manages three launchd-supervised services, defined in a single registry:

| Service    | launchd label                    | Port   | Notes                                                            |
| ---------- | -------------------------------- | ------ | --------------------------------------------------------------- |
| `adapter`  | `com.user.amc-adapter`           | `8080` | The FastAPI adapter (source of truth).                          |
| `receiver` | `com.user.amc-webhook-receiver`  | `8090` | The [webhook receiver](../operations/webhook-receiver.md).      |
| `backup`   | `com.user.amc-backup`            | —      | Scheduled SQLite backup; launchd-only (no HTTP port).           |

Most commands accept a target list. The rules are uniform across every command:

- A bare list of names (`amc status adapter receiver`) operates on exactly those services.
- The keyword `all`, or no target at all, fans out to **all three** services in the stable order `adapter`, `receiver`, `backup`.
- A service may be named by either its **short name** (`adapter`) or its **full launchd label** (`com.user.amc-adapter`).
- An unknown name aborts the command with exit code `1` and the message `Unknown service: <name>. Known: adapter, receiver, backup, all.`

!!! note "New to AMC?"
    For an end-to-end install walkthrough (Full Disk Access, Automation prompts, `.env` setup), start with the [Setup Guide](../getting-started/index.md). This page is the command reference.

## Command reference

| Command          | Description                                                              | Example                              |
| ---------------- | ----------------------------------------------------------------------- | ------------------------------------ |
| `amc serve`      | Run a service in the foreground (dev / debugging).                      | `amc serve adapter`                  |
| `amc install`    | Render plists and bootstrap launchd services.                           | `amc install adapter receiver`       |
| `amc uninstall`  | Bootout services and remove their plists.                               | `amc uninstall --keep-plist`         |
| `amc service`    | launchd lifecycle: `start`/`stop`/`restart`/`enable`/`disable`.         | `amc service restart adapter`        |
| `amc status`     | Show launchd state as a table or JSON.                                  | `amc status --json`                  |
| `amc logs`       | Tail-follow service logs.                                               | `amc logs adapter -n 100`            |
| `amc doctor`     | Run preflight diagnostics.                                              | `amc doctor`                         |

### `amc serve {adapter|receiver}`

Run a single service in the **foreground** — useful for development or debugging where you want logs on the terminal and direct signal handling.

```bash
amc serve adapter                 # bind 127.0.0.1:8080
amc serve receiver --port 9090    # override the receiver port
```

`serve` uses `os.execvp` to **replace** the current CLI process with `uv run uvicorn ...`. This keeps the process tree flat (no wrapper PID), so `Ctrl-C` / `SIGINT` is delivered straight to uvicorn rather than relayed through an intermediate Python process.

| Option   | Default                                  | Description            |
| -------- | ---------------------------------------- | ---------------------- |
| `--host` | `127.0.0.1`                              | Override the bind host. |
| `--port` | `8080` (adapter), `8090` (receiver)      | Override the bind port. |

!!! warning "`backup` is launchd-only"
    `amc serve backup` is rejected — the backup job runs only under launchd. Use `amc service start backup` instead.

### `amc install [name|all]`

Render the plist templates and bring services under launchd. With no arguments it installs **all** services.

```bash
amc install                       # install all three services
amc install adapter receiver      # install a subset
```

For each target, `install`:

1. Reads the plist template under `ops/launchd/` and substitutes the install directory and `$HOME`.
2. Validates the rendered plist with `plutil -lint`.
3. Writes it atomically into `~/Library/LaunchAgents/`.
4. Bootstraps it into the GUI user domain and marks it enabled.

The command is **idempotent** — it unloads any existing copy first, so re-running it safely re-installs.

### `amc uninstall [name|all] [--keep-plist]`

Bootout services and remove their plists. With no arguments it uninstalls **all** services.

```bash
amc uninstall                     # bootout + remove every plist
amc uninstall adapter --keep-plist  # bootout adapter, keep its plist on disk
```

| Option         | Description                                                  |
| -------------- | ----------------------------------------------------------- |
| `--keep-plist` | Bootout the service but leave its plist file on disk.       |

`uninstall` continues across all targets even if one fails, and returns the **worst** (maximum) per-target exit code.

### `amc service {start|stop|restart|enable|disable} [name|all]`

The launchd lifecycle verbs. Each defaults to `all`.

```bash
amc service start                 # start all services
amc service restart adapter       # restart one
amc service stop receiver
amc service disable backup        # leave the plist on disk, just disable it
```

| Verb       | launchctl mechanism                                                              |
| ---------- | ------------------------------------------------------------------------------- |
| `start`    | `launchctl kickstart` — auto-bootstraps if the plist exists but isn't loaded.   |
| `stop`     | `launchctl kill SIGTERM` — graceful stop.                                        |
| `restart`  | `launchctl kickstart -k` — kill and re-launch.                                   |
| `enable`   | `launchctl enable` — clear the disabled flag (plist stays on disk).             |
| `disable`  | `launchctl disable` — set the disabled flag (plist stays on disk).             |

### `amc status [name|all] [--json]`

Show launchd state — `loaded`, `enabled`, `pid`, last exit code, and uptime — as a Rich table (or plain table when piped), or as a JSON array.

```bash
amc status                        # all services, table
amc status adapter                # one row
amc status --json                 # machine-readable JSON array
```

Exit code is `0` when every targeted service is loaded, and `2` if any is not loaded.

### `amc logs [name|all] [--launchd] [--no-follow] [-n N]`

Tail-follow service logs. By default it prints the last 50 lines of today's app log and then follows it. Press `Ctrl-C` to exit (exit code `0`).

```bash
amc logs adapter                  # follow today's adapter log
amc logs adapter -n 100           # last 100 lines, then follow
amc logs adapter --no-follow      # print and exit (no follow)
amc logs --launchd adapter        # launchd-level stdout/stderr
amc logs all                      # interleave across all services
```

| Option          | Default | Description                                                                            |
| --------------- | ------- | ------------------------------------------------------------------------------------- |
| `-n`, `--lines` | `50`    | Trailing lines to emit before following.                                              |
| `--no-follow`   | off     | Print existing content and exit (no tail-follow).                                     |
| `--launchd`     | off     | Read the launchd-level stdout/stderr files instead of the app logs.                   |

App logs live at `~/Library/Logs/messaging-agent/<name>-YYYY-MM-DD.log`. The `--launchd` flag switches to the launchd-level stdout/stderr files instead — use these to catch process-spawn crashes that never reach the app logger. In multi-service mode, each line is prefixed with `[<svc>]`.

### `amc doctor [--json]`

Run preflight diagnostics over the install. Prints a Rich table (or plain when piped), or a JSON array of result objects.

```bash
amc doctor                        # table
amc doctor --json                 # JSON for tooling / screen readers
```

Exit code is `0` when every check is `ok` or `warn`, and `2` if any check is `fail`. The full check list is in the **`amc doctor` checks** section below.

## Exit codes

Most commands follow the same convention. The aggregate exit code across multiple targets is the **maximum** (worst) of the per-target codes.

| Code | Meaning                                                                                  |
| ---- | ---------------------------------------------------------------------------------------- |
| `0`  | Success — all targeted operations succeeded (and, for `status`, all services loaded).    |
| `1`  | Unknown service name.                                                                     |
| `2`  | launchctl, `plutil -lint`, or a `doctor`/`status` check failed.                          |
| `3`  | `install` only — `~/Library/LaunchAgents/` is not writable.                              |

## `amc doctor` checks

`doctor` runs the following checks in order. Any `fail` makes the command exit `2`; `warn` does not.

| Check                  | Verifies                                                                                          |
| ---------------------- | ------------------------------------------------------------------------------------------------ |
| `uv on PATH`           | The `uv` binary is on `PATH` (the launchd plists and `amc serve` invoke it).                     |
| `env file readable`    | `~/.config/messaging-agent/.env` exists and is readable.                                          |
| `chat.db readable (FDA)` | The CLI can read `chat.db` — a soft `warn`. See the note below.                                 |
| `plist templates present` | The plist templates exist under `ops/launchd/`.                                                |
| `plists installed`     | The rendered plists exist under `~/Library/LaunchAgents/`.                                        |
| `services bootstrapped` | Each service is bootstrapped (`launchctl print gui/<uid>/<label>` succeeds).                     |
| `port 8080 free`       | The adapter port is available.                                                                    |
| `port 8090 free`       | The receiver port is available.                                                                   |
| `log directory writable` | `~/Library/Logs/messaging-agent/` exists and is writable.                                       |

!!! info "Why Full Disk Access is only a `warn`"
    The `chat.db readable (FDA)` check tests whether the **CLI binary** has Full Disk Access. But the real principal that needs FDA is the launchd-spawned adapter, not the CLI. The CLI usually lacks FDA, which is normal — so doctor reports a `warn` with an explanatory message rather than failing.

For configuration details see [Configuration](configuration.md); for recovery steps when a check fails, see the [Runbook](../operations/runbook.md).

## Shell completion

`amc` exposes Typer's built-in shell completion. Typer auto-detects your shell via [shellingham](https://github.com/sarugaku/shellingham).

```bash
amc --install-completion zsh      # install for zsh, then run: exec zsh
amc --install-completion bash     # bash
amc --install-completion fish     # fish
amc --show-completion zsh         # print the script without installing
```

After installing for `zsh`, reload your shell so completion takes effect:

```bash
exec zsh
```

## Design note

!!! note "Cold-start budget under 200 ms"
    `amc` is built to feel instant. `amc.cli.app` imports only Typer plus light stdlib at module scope, and **defers** heavy dependencies — FastAPI, SQLAlchemy, Rich tables, and the CLI's own helper modules — to inside each command body. The shell-out modules (`launchctl`, `ps`) wrap their subprocess calls behind a small, testable seam so the lifecycle commands stay fast and unit-testable.

## See also

- [Setup Guide](../getting-started/index.md) — full install walkthrough.
- [Operations overview](../operations/index.md) — running AMC day to day.
- [Webhook Receiver](../operations/webhook-receiver.md) — the `receiver` service.
- [Runbook](../operations/runbook.md) — recovery procedures.
- [Configuration](configuration.md) — environment variables.
