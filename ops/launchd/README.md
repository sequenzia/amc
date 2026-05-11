# launchd supervision for the AMC services

This directory holds the launchd LaunchAgents that supervise the AMC adapter
and the optional webhook receiver on macOS, per spec §7.2 (Tech Stack), §6.4
(Reliability / RTO), and §11.1 (Deployment Strategy). The full operator
runbook lives in `SETUP.md` (Phase 4) and `RUNBOOK.md`; this README covers
only the launchd artifacts.

Day-to-day ops are driven through the **`amc` CLI**, which wraps `launchctl`
so operators don't have to memorize domain-target strings or plist paths.

## What is launchd?

`launchd` is macOS's per-user / system-wide process supervisor. A
LaunchAgent (one of the artifacts here) is a property-list file in
`~/Library/LaunchAgents/` that tells `launchd` how to start, restart, and
log a long-running process for the logged-in user.

## Services

| Service name (as known to `amc`) | launchd label | Role |
|----------------------------------|----------------|------|
| `adapter` | `com.user.amc-adapter` | The FastAPI adapter. Runs the iMessage + Discord connectors, exposes the REST + webhook surface on `127.0.0.1:8080`. |
| `receiver` | `com.user.amc-webhook-receiver` | The webhook bridge (`webhook-receiver/`). Listens on `127.0.0.1:8090` and translates outbound adapter webhooks into one-shot `claude -p` invocations. |
| `backup` | `com.user.amc-backup` | Periodic SQLite snapshot job. Defined for completeness; the run script is provisional. |

## Files

| File | Role |
|------|------|
| `com.user.amc-adapter.plist` | Template plist with `__INSTALL_DIR__` and `__HOME__` placeholders. `amc install` renders it into `~/Library/LaunchAgents/`. |
| `run-adapter.sh` | Wrapper script invoked by launchd. `cd`s to the install dir and `exec`s `uv run uvicorn amc.app:app --host 127.0.0.1 --port 8080`. Avoids hard-coding a Python path. |
| `com.user.amc-webhook-receiver.plist` | Template plist for the webhook-receiver workspace member. |
| `run-webhook-receiver.sh` | Wrapper that `exec`s `uv run --project webhook-receiver uvicorn amc_receiver.app:app --host 127.0.0.1 --port 8090`. |
| `com.user.amc-backup.plist` | Template plist for the backup job. |

## Install / uninstall

```bash
amc install                  # install every known service
amc install adapter receiver # install only specific services by name
amc uninstall                # remove every installed service
amc uninstall adapter        # remove just one service
amc uninstall --keep-plist   # bootout but leave the plist file on disk
```

`amc install` is idempotent: it renders the template, runs `plutil -lint`,
atomically writes the plist into `~/Library/LaunchAgents/`, and bootstraps
the service. Re-running picks up template changes safely.

## Lifecycle

```bash
amc service start adapter    # bootstrap + kickstart
amc service stop adapter     # SIGTERM + bootout
amc service restart adapter  # kickstart -k
amc service enable adapter   # mark allowed-to-load across reboots
amc service disable adapter
amc service start all        # operate on every installed service at once
```

## Status

```bash
amc status                   # human-readable table for every service
amc status adapter           # single service
amc status --json            # machine-readable; suitable for monitoring
```

The status table surfaces `state`, `pid`, `last_exit`, `uptime`, and (where
launchd exposes it) the spawn time.

## Logs

```bash
amc logs adapter             # follow today's structured app log
amc logs adapter --no-follow # print last N lines and exit
amc logs adapter -n 200      # last 200 lines
amc logs adapter --launchd   # follow the launchd-level stdout/stderr instead
amc logs all                 # multi-service follow; lines prefixed with [svc]
```

The plist directs launchd-level stdout and stderr to:

- `~/Library/Logs/messaging-agent/launchd-stdout.log`             (adapter)
- `~/Library/Logs/messaging-agent/launchd-stderr.log`             (adapter)
- `~/Library/Logs/messaging-agent/launchd-receiver-stdout.log`    (receiver)
- `~/Library/Logs/messaging-agent/launchd-receiver-stderr.log`    (receiver)

These capture process spawn / crash output. The applications' structured
JSON logs are written to the same directory with daily rotation:
`adapter-YYYY-MM-DD.log` from `amc.core.logging` (see spec §11.2
`AMC_LOG_DIR`) and `receiver-YYYY-MM-DD.log` from `amc_receiver.logging`.

## Diagnostics

```bash
amc doctor                   # run permission + config + service checks
amc doctor --json            # same, machine-readable
```

`amc doctor` is the first thing to reach for when something is wrong. It
checks Full Disk Access, Automation grants, `.env` presence + mode,
allowlist presence, plist install state, launchd service state, the
adapter's `/healthz` endpoint, and the backup script.

## Configuration

The plist intentionally sets `EnvironmentVariables` to an empty dict. The
adapter loads its own configuration from `~/.config/messaging-agent/.env`
(see spec §11.2). Do not put `AMC_*` values in the plist — keeping them in
the `.env` file means a config change only needs `amc service restart
adapter`, not a reinstall.

## Restart policy

- `RunAtLoad`: true — start at login / `amc install`.
- `KeepAlive`: `{ SuccessfulExit: false }` — restart the adapter on crash
  or non-zero exit, but do not loop on a clean shutdown (`exit 0`).
- `ThrottleInterval`: 10 seconds — minimum delay between respawns to avoid
  busy-spinning a crash loop.

This satisfies the §6.4 RTO target of ≤ 5 minutes after a process crash.

## Updates

Per spec §11.1:

```bash
git pull && uv sync --all-packages && uv run alembic upgrade head
amc service restart adapter
```

Restarting in place is enough; reinstalling the plist is only required
when the plist template itself changes — and `amc install` is idempotent
and safe to re-run.

## Escape hatch: raw `launchctl`

Routine ops never need raw `launchctl`. If `amc` itself is broken or the
host has wedged into an unrecoverable launchd state (e.g. ghost services
that `amc uninstall` can't dislodge), you can drop down to the underlying
domain commands:

```bash
# Diagnostic only — prefer amc status / amc service equivalents.
launchctl print "gui/$(id -u)/com.user.amc-adapter"
launchctl bootout "gui/$(id -u)/com.user.amc-adapter"
```

Use sparingly and file an issue describing what `amc` couldn't recover
from — gaps there are bugs.
