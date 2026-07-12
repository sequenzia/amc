# `amg` CLI

The `amg` command is the operator-facing CLI that ships with the AMG adapter (built with [Typer](https://typer.tiangolo.com/) + [Rich](https://rich.readthedocs.io/), package `amg/cli/`). It is everything you need to install, inspect, and troubleshoot AMG on a single Mac: it wraps the foreground service entry points, the launchd plist render/install pipeline, and preflight diagnostics.

After `uv sync --all-packages`, the `amg` entry point is on your `PATH`.

```bash
uv sync --all-packages
amg --help
amg --version
```

## Overview

`amg` manages three launchd-supervised services, defined in a single registry:

| Service    | launchd label                    | Port   | Notes                                                            |
| ---------- | -------------------------------- | ------ | --------------------------------------------------------------- |
| `adapter`  | `com.user.amg-adapter`           | `8080` | The FastAPI adapter (source of truth).                          |
| `receiver` | `com.user.amg-webhook-receiver`  | `8090` | The [webhook receiver](../operations/webhook-receiver.md).      |
| `backup`   | `com.user.amg-backup`            | —      | Scheduled SQLite backup; launchd-only (no HTTP port).           |

Most commands accept a target list. The rules are uniform across every command:

- A bare list of names (`amg status adapter receiver`) operates on exactly those services.
- The keyword `all`, or no target at all, fans out to **all three** services in the stable order `adapter`, `receiver`, `backup`.
- A service may be named by either its **short name** (`adapter`) or its **full launchd label** (`com.user.amg-adapter`).
- An unknown name aborts the command with exit code `1` and the message `Unknown service: <name>. Known: adapter, receiver, backup, all.`

!!! note "New to AMG?"
    For an end-to-end install walkthrough (Full Disk Access, Automation prompts, `.env` setup), start with the [Setup Guide](../getting-started/index.md). This page is the command reference.

## How install, enable, and start differ

This is the single most common point of confusion, so it is worth being precise about. A service can be in **four independent states**, and a different pair of commands controls each one. `amg install` looks like one action, but it actually sets three of these states at once — which is exactly why `install`/`uninstall` and `service enable`/`service disable` are so easy to conflate.

| State | The question it answers | Controlled by | launchctl underneath |
| ----- | ----------------------- | ------------- | -------------------- |
| **On disk** | Is the plist file present in `~/Library/LaunchAgents/`? | `install` creates · `uninstall` removes | file write / `rm` |
| **Loaded** | Is launchd *aware* of the service — registered in the GUI domain? | `install` loads · `uninstall` unloads | `bootstrap` / `bootout` |
| **Enabled** | A *persistent* flag: is the service *permitted* to run at all? | `service enable` · `service disable` | `enable` / `disable` |
| **Running** | Is the process alive *right now*? | `service start` · `stop` · `restart` | `kickstart` / `kill` |

These axes are independent: a service can be on disk but not loaded, loaded but disabled, enabled but not running, and so on. The two pairs people mix up sit on different axes entirely.

**`install` / `uninstall` — does the service exist on this Mac?** `amg install` does the whole onboarding in one shot: render and write the plist **to disk**, **enable** it (clearing any leftover disable flag), then **bootstrap** it into launchd — and because the plists set `RunAtLoad`, that also starts it. `amg uninstall` is the reverse: `bootout` (unload from launchd) and delete the plist. Think of it as *installing* versus *trashing* an app.

**`service enable` / `service disable` — a persistent on/off switch.** `disable` does **not** delete anything and does **not** stop a running process. It writes a sticky "do not run" bit into launchd's own database (`/var/db/com.apple.xpc.launchd/disabled.<uid>.plist`). That bit survives reboots, survives `bootout`, and even survives `amg uninstall`. `enable` simply clears it. Neither verb loads, unloads, or starts the service — they only flip the flag, which takes effect the next time launchd tries to load the service.

!!! tip "An analogy"
    Picture a Mac app. **install / uninstall** is copying the app into `/Applications` versus dragging it to the Trash. **enable / disable** is the **"Open at Login"** checkbox — unchecking it does not delete the app, it just says "don't let this run," and it stays that way until you re-check it. **start / stop** is double-clicking the app versus quitting it right now.

### Which command should I use?

| I want to… | Command |
| ---------- | ------- |
| Set a service up for the first time | `amg install` |
| Remove a service for good | `amg uninstall` |
| Park an installed service across reboots, then bring it back later without re-installing | `amg service disable` … later `amg service enable` |
| Stop or start a service for now (it still returns at next login via `RunAtLoad`) | `amg service stop` / `amg service start` |

!!! warning "The disable flag outlives `uninstall`"
    Because `disable` writes a *persistent* flag and `uninstall` only does `bootout` + plist removal, a stale disable flag can linger after a service is long gone. While that flag is set, `launchctl bootstrap` refuses the service with the opaque message `Bootstrap failed: 5: Input/output error`. `amg install` guards against this by always running `enable` **before** `bootstrap`, so a fresh install clears any stale flag for you. Note that `amg service start` does **not** clear the flag — only `enable` and `install` do — so a disabled service must be `enable`d (or re-`install`ed) before it will start.

## Command reference

| Command          | Description                                                              | Example                              |
| ---------------- | ----------------------------------------------------------------------- | ------------------------------------ |
| `amg serve`      | Run a service in the foreground (dev / debugging).                      | `amg serve adapter`                  |
| `amg install`    | Render plists and bootstrap launchd services.                           | `amg install adapter receiver`       |
| `amg uninstall`  | Bootout services and remove their plists.                               | `amg uninstall --keep-plist`         |
| `amg service`    | launchd lifecycle: `start`/`stop`/`restart`/`enable`/`disable`.         | `amg service restart adapter`        |
| `amg status`     | Show launchd state as a table or JSON.                                  | `amg status --json`                  |
| `amg logs`       | Tail-follow service logs.                                               | `amg logs adapter -n 100`            |
| `amg doctor`     | Run preflight diagnostics.                                              | `amg doctor`                         |

### `amg serve {adapter|receiver}`

Run a single service in the **foreground** — useful for development or debugging where you want logs on the terminal and direct signal handling.

```bash
amg serve adapter                 # bind 127.0.0.1:8080
amg serve receiver --port 9090    # override the receiver port
```

`serve` uses `os.execvp` to **replace** the current CLI process with `uv run uvicorn ...`. This keeps the process tree flat (no wrapper PID), so `Ctrl-C` / `SIGINT` is delivered straight to uvicorn rather than relayed through an intermediate Python process.

| Option   | Default                                  | Description            |
| -------- | ---------------------------------------- | ---------------------- |
| `--host` | `127.0.0.1`                              | Override the bind host. |
| `--port` | `8080` (adapter), `8090` (receiver)      | Override the bind port. |

!!! warning "`backup` is launchd-only"
    `amg serve backup` is rejected — the backup job runs only under launchd. Use `amg service start backup` instead.

### `amg install [name|all]`

Render the plist templates and bring services under launchd. With no arguments it installs **all** services.

```bash
amg install                       # install all three services
amg install adapter receiver      # install a subset
```

For each target, `install`:

1. Reads the plist template under `ops/launchd/` and substitutes the install directory and `$HOME`.
2. Validates the rendered plist with `plutil -lint`.
3. Writes it atomically into `~/Library/LaunchAgents/`.
4. **Enables** it (clearing any persistent disable flag), then **bootstraps** it into the GUI user domain; `RunAtLoad` starts it. Enable must come **before** bootstrap — a stale disable flag makes `bootstrap` fail with `5: Input/output error`. See [How install, enable, and start differ](#how-install-enable-and-start-differ).

The command is **idempotent** — if a copy is already loaded it boots it out first (and waits for that teardown to finish before re-bootstrapping, since `bootout` is asynchronous), so re-running safely re-installs.

### `amg uninstall [name|all] [--keep-plist]`

Bootout services and remove their plists. With no arguments it uninstalls **all** services.

```bash
amg uninstall                     # bootout + remove every plist
amg uninstall adapter --keep-plist  # bootout adapter, keep its plist on disk
```

| Option         | Description                                                  |
| -------------- | ----------------------------------------------------------- |
| `--keep-plist` | Bootout the service but leave its plist file on disk.       |

`uninstall` continues across all targets even if one fails, and returns the **worst** (maximum) per-target exit code.

!!! note "`uninstall` does not clear the disable flag"
    `uninstall` only unloads the service and removes its plist. A persistent disable flag set by `amg service disable` survives — see [How install, enable, and start differ](#how-install-enable-and-start-differ). This is harmless: the next `amg install` clears it for you.

### `amg service {start|stop|restart|enable|disable} [name|all]`

The launchd lifecycle verbs. Each defaults to `all`.

```bash
amg service start                 # start all services
amg service restart adapter       # restart one
amg service stop receiver
amg service disable backup        # leave the plist on disk, just disable it
```

| Verb       | launchctl mechanism                                                              |
| ---------- | ------------------------------------------------------------------------------- |
| `start`    | `launchctl kickstart` — auto-bootstraps if the plist exists but isn't loaded.   |
| `stop`     | `launchctl kill SIGTERM` — graceful stop.                                        |
| `restart`  | `launchctl kickstart -k` — kill and re-launch.                                   |
| `enable`   | `launchctl enable` — clear the [persistent disable flag](#how-install-enable-and-start-differ). Does not load or start; plist stays on disk. |
| `disable`  | `launchctl disable` — set the [persistent disable flag](#how-install-enable-and-start-differ), blocking future loads. Does not stop a running process; plist stays on disk. |

### `amg status [name|all] [--json]`

Show launchd state — `loaded`, `enabled`, `pid`, last exit code, and uptime — as a Rich table (or plain table when piped), or as a JSON array.

```bash
amg status                        # all services, table
amg status adapter                # one row
amg status --json                 # machine-readable JSON array
```

Exit code is `0` when every targeted service is loaded, and `2` if any is not loaded.

### `amg logs [name|all] [--launchd] [--no-follow] [-n N]`

Tail-follow service logs. By default it prints the last 50 lines of today's app log and then follows it. Press `Ctrl-C` to exit (exit code `0`).

```bash
amg logs adapter                  # follow today's adapter log
amg logs adapter -n 100           # last 100 lines, then follow
amg logs adapter --no-follow      # print and exit (no follow)
amg logs --launchd adapter        # launchd-level stdout/stderr
amg logs all                      # interleave across all services
```

| Option          | Default | Description                                                                            |
| --------------- | ------- | ------------------------------------------------------------------------------------- |
| `-n`, `--lines` | `50`    | Trailing lines to emit before following.                                              |
| `--no-follow`   | off     | Print existing content and exit (no tail-follow).                                     |
| `--launchd`     | off     | Read the launchd-level stdout/stderr files instead of the app logs.                   |

App logs live at `~/Library/Logs/messaging-agent/<name>-YYYY-MM-DD.log`. The `--launchd` flag switches to the launchd-level stdout/stderr files instead — use these to catch process-spawn crashes that never reach the app logger. In multi-service mode, each line is prefixed with `[<svc>]`.

### `amg doctor [--json]`

Run preflight diagnostics over the install. Prints a Rich table (or plain when piped), or a JSON array of result objects.

```bash
amg doctor                        # table
amg doctor --json                 # JSON for tooling / screen readers
```

Exit code is `0` when every check is `ok` or `warn`, and `2` if any check is `fail`. The full check list is in the **`amg doctor` checks** section below.

## Exit codes

Most commands follow the same convention. The aggregate exit code across multiple targets is the **maximum** (worst) of the per-target codes.

| Code | Meaning                                                                                  |
| ---- | ---------------------------------------------------------------------------------------- |
| `0`  | Success — all targeted operations succeeded (and, for `status`, all services loaded).    |
| `1`  | Unknown service name.                                                                     |
| `2`  | launchctl, `plutil -lint`, or a `doctor`/`status` check failed.                          |
| `3`  | `install` only — `~/Library/LaunchAgents/` is not writable.                              |

## `amg doctor` checks

`doctor` runs the following checks in order. Any `fail` makes the command exit `2`; `warn` does not.

| Check                  | Verifies                                                                                          |
| ---------------------- | ------------------------------------------------------------------------------------------------ |
| `uv on PATH`           | The `uv` binary is on `PATH` (the launchd plists and `amg serve` invoke it).                     |
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

`amg` exposes Typer's built-in shell completion. Typer auto-detects your shell via [shellingham](https://github.com/sarugaku/shellingham).

```bash
amg --install-completion zsh      # install for zsh, then run: exec zsh
amg --install-completion bash     # bash
amg --install-completion fish     # fish
amg --show-completion zsh         # print the script without installing
```

After installing for `zsh`, reload your shell so completion takes effect:

```bash
exec zsh
```

## Design note

!!! note "Cold-start budget under 200 ms"
    `amg` is built to feel instant. `amg.cli.app` imports only Typer plus light stdlib at module scope, and **defers** heavy dependencies — FastAPI, SQLAlchemy, Rich tables, and the CLI's own helper modules — to inside each command body. The shell-out modules (`launchctl`, `ps`) wrap their subprocess calls behind a small, testable seam so the lifecycle commands stay fast and unit-testable.

## See also

- [Setup Guide](../getting-started/index.md) — full install walkthrough.
- [Operations overview](../operations/index.md) — running AMG day to day.
- [Webhook Receiver](../operations/webhook-receiver.md) — the `receiver` service.
- [Runbook](../operations/runbook.md) — recovery procedures.
- [Configuration](configuration.md) — environment variables.
