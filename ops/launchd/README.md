# launchd supervision for the AMC services

This directory contains the launchd LaunchAgents that supervise the AMC
adapter and the optional webhook receiver on macOS, per spec §7.2 (Tech
Stack), §6.4 (Reliability / RTO), and §11.1 (Deployment Strategy). The
full operator runbook lives in `SETUP.md` (Phase 4); this README covers
only the launchd artifacts.

## Files

| File | Role |
|------|------|
| `com.user.amc-adapter.plist` | Template plist with `__INSTALL_DIR__` and `__HOME__` placeholders. `install.sh` renders it into `~/Library/LaunchAgents/`. |
| `run-adapter.sh` | Wrapper script invoked by launchd. `cd`s to the install dir and `exec`s `uv run uvicorn amc.app:app --host 127.0.0.1 --port 8080`. Avoids hard-coding a Python path. |
| `com.user.amc-webhook-receiver.plist` | Template plist for the webhook-receiver workspace member. Bridges adapter outbound webhooks to one-shot `claude -p` invocations. |
| `run-webhook-receiver.sh` | Wrapper that `exec`s `uv run --project webhook-receiver uvicorn amc_receiver.app:app --host 127.0.0.1 --port 8090`. |
| `install.sh` | Idempotent installer. Installs both services by default; pass labels to install only specific ones. Renders the plist(s), validates with `plutil -lint`, copies into `~/Library/LaunchAgents/`, and `(re)bootstraps` each. |

## Install

```bash
cd <install-dir>                                    # the AMC repo root
./ops/launchd/install.sh                            # both services
./ops/launchd/install.sh com.user.amc-adapter      # adapter only
./ops/launchd/install.sh com.user.amc-webhook-receiver  # receiver only
```

Re-running `install.sh` is safe: it unloads any existing version of the
service before loading the new one.

Verify the services are loaded:

```bash
launchctl print "gui/$(id -u)/com.user.amc-adapter"
launchctl print "gui/$(id -u)/com.user.amc-webhook-receiver"
```

## Logs

The plist directs stdout and stderr to:

- `~/Library/Logs/messaging-agent/launchd-stdout.log`             (adapter)
- `~/Library/Logs/messaging-agent/launchd-stderr.log`             (adapter)
- `~/Library/Logs/messaging-agent/launchd-receiver-stdout.log`    (receiver)
- `~/Library/Logs/messaging-agent/launchd-receiver-stderr.log`    (receiver)

These are launchd-level logs (process spawn / crash output). The
applications' own structured JSON logs are written to the same directory
with daily rotation: `adapter-YYYY-MM-DD.log` from `amc.core.logging`
(see spec §11.2 `AMC_LOG_DIR`) and `receiver-YYYY-MM-DD.log` from
`amc_receiver.logging`.

Tail both:

```bash
tail -F ~/Library/Logs/messaging-agent/launchd-stdout.log \
        ~/Library/Logs/messaging-agent/launchd-stderr.log
```

## Configuration

The plist intentionally sets `EnvironmentVariables` to an empty dict. The
adapter loads its own configuration from `~/.config/messaging-agent/.env`
(see spec §11.2). Do not put `AMC_*` values in the plist — keeping them in
the `.env` file means a config change does not require reinstalling the
LaunchAgent.

## Restart policy

- `RunAtLoad`: true — start at login / `launchctl bootstrap`.
- `KeepAlive`: `{ SuccessfulExit: false }` — restart the adapter on crash
  or non-zero exit, but do not loop on a clean shutdown (`exit 0`).
- `ThrottleInterval`: 10 seconds — minimum delay between respawns to avoid
  busy-spinning a crash loop.

This satisfies the §6.4 RTO target of ≤ 5 minutes after a process crash.

## Uninstall

```bash
launchctl bootout "gui/$(id -u)/com.user.amc-adapter"
rm ~/Library/LaunchAgents/com.user.amc-adapter.plist
```

## Updates

Per spec §11.1:

```bash
git pull && uv sync && alembic upgrade head
launchctl kickstart -k "gui/$(id -u)/com.user.amc-adapter"
```

`kickstart -k` restarts the running service in place; reinstalling the
plist is only needed when the plist itself changes.
