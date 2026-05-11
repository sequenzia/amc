# amc-cli PRD

**Version**: 1.0
**Author**: Stephen Sequenzia
**Date**: 2026-05-10
**Status**: Draft
**Spec Type**: New feature
**Spec Depth**: Detailed specifications
**Description**: A Python-based (Typer) CLI that manages AMC — start adapter and webhook-receiver manually, install/uninstall them as launchd services, check status, enable/disable/restart, and view logs.

---

## 1. Executive Summary

`amc` is a Typer-based command-line tool that consolidates AMC operations behind a single, discoverable interface. It replaces ad-hoc shell commands (`uv run uvicorn …`, `./ops/launchd/install.sh`, `launchctl bootout …`, `tail -F …`) with a uniform verb-noun surface for installing, supervising, and inspecting the AMC adapter, the webhook receiver, and the backup launchd service.

## 2. Problem Statement

### 2.1 The Problem
AMC operators (currently a single user) drive every operational task through raw shell. Starting a service manually requires the right `uv run uvicorn` incantation; installing the launchd agents requires `ops/launchd/install.sh`; status requires `launchctl print "gui/$(id -u)/com.user.amc-adapter"`; logs require remembering the full path under `~/Library/Logs/messaging-agent/`. There is no single tool that knows about all three services and their idioms.

### 2.2 Current State
- Manual `uv run uvicorn amc.app:app --host 127.0.0.1 --port 8080` for the adapter and `uv run --project webhook-receiver uvicorn amc_receiver.app:app --host 127.0.0.1 --port 8090` for the receiver.
- `ops/launchd/install.sh` (bash) handles plist rendering, `plutil -lint`, `bootstrap`, and `enable`. It accepts optional label arguments to install only one service.
- Uninstall, restart, enable/disable, and log tailing are not scripted — operators copy/paste the canonical `launchctl` and `tail` commands from `ops/launchd/README.md` and `RUNBOOK.md`.
- Three launchd services exist: `com.user.amc-adapter`, `com.user.amc-webhook-receiver`, `com.user.amc-backup`.

### 2.3 Impact Analysis
- Error-prone: every command embeds the service label, install path, and log location. Typos cause silent no-ops.
- Discovery cost: new contributors (or the operator returning after a break) must read four files (`SETUP.md`, `RUNBOOK.md`, `ops/launchd/README.md`, the install.sh source) to perform routine ops.
- Drift risk: install.sh's plist substitution logic is the only place that knows about `__INSTALL_DIR__` / `__HOME__`. Any new operation (uninstall, restart) has to relearn the service registry.

### 2.4 Business Value
A single-operator CLI is a productivity multiplier for the AMC operator, removes a class of typo-induced incidents, and creates a clean home for future ops (e.g., service-aware health checks, smoke tests) that today have nowhere to land.

## 3. Goals & Success Metrics

### 3.1 Primary Goals
1. Replace every service-ops command in `RUNBOOK.md` and `ops/launchd/README.md` with a single `amc <cmd>` invocation.
2. Make service install/uninstall fully scripted and idempotent without touching bash.
3. Provide one-command status and log tailing across all three services.
4. Surface common misconfigurations (missing FDA, missing `.env`, port collisions) via `amc doctor`.

### 3.2 Success Metrics

| Metric | Current Baseline | Target | Measurement Method | Timeline |
|--------|------------------|--------|--------------------|----------|
| Service ops commands documented as raw shell | ~12 across RUNBOOK/READMEs | 0 (all routed through `amc`) | Grep docs for `launchctl`, `tail -F`, `uvicorn` after migration | Phase 4 complete |
| Manual lookups per ops task | 1–2 file reads | 0 (CLI is self-describing via `--help`) | Operator audit | Phase 4 complete |
| Time to install all three services on a fresh machine | ~2 min (read + paste install.sh) | <30 s (`amc install`) | Stopwatch on clean machine | Phase 2 complete |

### 3.3 Non-Goals
- Multi-operator orchestration; a single operator runs all services on one Mac.
- Cross-platform support; the CLI is macOS-only.
- Configuration editing; the `.env` file remains hand-managed.
- Database / migration commands; `alembic` calls remain direct.

## 4. User Research

### 4.1 Target Users

#### Primary Persona: AMC Operator
- **Role/Description**: The single user who installs, runs, and monitors AMC on their personal Mac. Same person who builds the system. Comfortable with shell but actively trying to reduce the number of incantations they must remember.
- **Goals**: Start/stop services without thinking; verify "is it running?" in one command; quickly see what crashed and why.
- **Pain Points**: Multi-line `launchctl` commands; remembering the `gui/$(id -u)/<label>` domain prefix; locating the right log file among four files per service.
- **Context**: Mac terminal, every working day. Often in the middle of debugging — wants the CLI to stay out of the way.

### 4.2 User Journey Map

```
Fresh clone --> amc install --> amc doctor --> amc service start all --> amc status
       --> amc logs adapter --> (debug)
       --> amc service restart adapter
       --> amc service disable receiver (temporary pause)
       --> amc uninstall (decommission)
```

The CLI mirrors the operator's mental model: install once, start, watch, restart on change, uninstall when retiring the install.

## 5. Functional Requirements

### 5.1 Feature: Service Registry

**Priority**: P0 (Critical)

#### User Stories
**US-001**: As the operator, I want a single source of truth for service metadata (label, plist template, log paths, port) so that every command targets the same set of services consistently.

**Acceptance Criteria**:
- [ ] `amc/cli/services.py` defines a `Service` dataclass with: `name` (short id, e.g., `adapter`), `label` (full launchd label), `plist_template` (path under `ops/launchd/`), `run_script` (path), `app_log_glob` (e.g., `adapter-*.log`), `launchd_stdout` / `launchd_stderr` paths, `port` (or `None`).
- [ ] Three entries registered: `adapter`, `receiver` (label `com.user.amc-webhook-receiver`), `backup`.
- [ ] Lookups by short name (`adapter`) and by label (`com.user.amc-adapter`) both supported.
- [ ] `all` keyword resolves to all three services in a stable order: `adapter`, `receiver`, `backup`.

**Edge Cases**:
- Unknown service name → exit code 1 with `Unknown service: <name>. Known: adapter, receiver, backup, all.`
- Plist file referenced in registry missing on disk → command that needs the plist exits 2 with `Plist not found: <path>`; `doctor` flags as a critical finding.

---

### 5.2 Feature: Manual Foreground Run (`amc serve`)

**Priority**: P0 (Critical)

#### User Stories
**US-002**: As the operator, I want to start the adapter or receiver in the foreground from one command so that I can run them ad hoc during development without touching launchd.

**Acceptance Criteria**:
- [ ] `amc serve adapter` execs `uv run uvicorn amc.app:app --host 127.0.0.1 --port 8080` in the foreground (process replaces the CLI via `os.execvp`).
- [ ] `amc serve receiver` execs `uv run --project webhook-receiver uvicorn amc_receiver.app:app --host 127.0.0.1 --port 8090`.
- [ ] `amc serve backup` is intentionally not implemented (backup runs only under launchd); attempting it exits 1 with a clear message.
- [ ] `--host` and `--port` flags override the defaults.
- [ ] Ctrl-C delivers SIGINT to the uvicorn process directly (no extra shell layer in between).

**Edge Cases**:
- Port in use → uvicorn fails fast; CLI does not intercept the error (uvicorn's traceback reaches stderr verbatim).
- launchd service is currently running → no auto-stop; `serve` will collide on the port and uvicorn will exit. (Doctor will warn; future enhancement could pre-check.)

---

### 5.3 Feature: Install (`amc install`)

**Priority**: P0 (Critical)

#### User Stories
**US-003**: As the operator, I want one command that renders, installs, and bootstraps the launchd plists for some or all services so that I never write `sed` / `plutil` / `launchctl bootstrap` by hand.

**Acceptance Criteria**:
- [ ] `amc install` (no args) installs all three services.
- [ ] `amc install adapter` installs only the adapter; `amc install adapter receiver` installs both.
- [ ] For each target service: read the plist template from `ops/launchd/<label>.plist`, substitute `__INSTALL_DIR__` (repo root) and `__HOME__` (`$HOME`), write to a temp file, `plutil -lint` it, and atomically `mv` to `~/Library/LaunchAgents/<label>.plist`.
- [ ] If the service is already bootstrapped, `launchctl bootout` first, then `bootstrap`, then `enable`.
- [ ] `~/Library/Logs/messaging-agent/` and `~/Library/LaunchAgents/` are created if missing.
- [ ] Idempotent: re-running has no observable difference except re-loading the plist.
- [ ] Exit 0 on success, 2 on any launchctl failure, 3 if `~/Library/LaunchAgents/` is not writable.
- [ ] `ops/launchd/install.sh` is **deleted**; `ops/launchd/README.md`, `RUNBOOK.md`, and `SETUP.md` are updated to reference `amc install`.

**Edge Cases**:
- `plutil -lint` fails on rendered output → leave temp file in place for debugging, exit 2 with `Rendered plist failed plutil -lint: <tmpfile>`.
- Existing plist with same name but unknown contents → overwrite (the operator's install dir is the source of truth).

---

### 5.4 Feature: Uninstall (`amc uninstall`)

**Priority**: P0 (Critical)

#### User Stories
**US-004**: As the operator, I want to remove launchd services and their plist files in one command so that decommissioning a service is a single atomic step.

**Acceptance Criteria**:
- [ ] `amc uninstall [name|all]` runs `launchctl bootout "gui/$(id -u)/<label>"` for each target service.
- [ ] By default also removes `~/Library/LaunchAgents/<label>.plist`.
- [ ] `--keep-plist` preserves the plist on disk (service unloaded but easy to re-enable later).
- [ ] Already-unloaded services do not cause errors; the command treats "not bootstrapped" as a success.
- [ ] Exit 0 on success, 2 on unexpected launchctl failure.

**Edge Cases**:
- Plist file missing → no error if service was already unloaded; warning emitted.
- Multiple targets, one fails → CLI continues with the rest; final exit code reflects worst error.

---

### 5.5 Feature: Lifecycle (`amc service …`)

**Priority**: P0 (Critical)

#### User Stories
**US-005**: As the operator, I want to start, stop, restart, enable, and disable any service (or all) with one verb per action so that I don't have to remember which `launchctl` subcommand maps to which intent.

**Acceptance Criteria**:
- [ ] `amc service start [name|all]` runs `launchctl kickstart "gui/$(id -u)/<label>"` (loads if not loaded, starts if not running).
- [ ] `amc service stop [name|all]` runs `launchctl kill SIGTERM "gui/$(id -u)/<label>"`.
- [ ] `amc service restart [name|all]` runs `launchctl kickstart -k "gui/$(id -u)/<label>"` (terminate + restart in place).
- [ ] `amc service enable [name|all]` runs `launchctl enable "gui/$(id -u)/<label>"` (next-load behavior; plist stays).
- [ ] `amc service disable [name|all]` runs `launchctl disable "gui/$(id -u)/<label>"`.
- [ ] Default target when name omitted: `all`.
- [ ] Per-service result printed; aggregate exit code = max of individual exit codes.

**Edge Cases**:
- Service not bootstrapped → `start` attempts to `bootstrap` first (using the installed plist) and retries `kickstart`. If still failing, exits 2 with `Service not installed; run amc install first`.
- `stop` on already-stopped service → exit 0, message `<name>: already stopped`.

---

### 5.6 Feature: Status (`amc status`)

**Priority**: P0 (Critical)

#### User Stories
**US-006**: As the operator, I want a one-line-per-service view of "is it loaded, enabled, running, PID, last exit, uptime" so that I can answer "is AMC healthy?" in under a second.

**Acceptance Criteria**:
- [ ] `amc status [name|all]` prints a Rich table with columns: `service`, `loaded`, `enabled`, `pid`, `last_exit`, `uptime`.
- [ ] Fields are extracted by parsing `launchctl print "gui/$(id -u)/<label>"` output.
- [ ] `loaded` = `yes`/`no` (boolean: was the service found in launchctl print).
- [ ] `enabled` = `yes`/`no` (read from the disabled state in `launchctl print-disabled gui/$(id -u)`).
- [ ] `pid` = numeric PID or `-` if not running.
- [ ] `last_exit` = exit code from `last exit code = N` line, or `-` if never run.
- [ ] `uptime` = human-readable (`3m 12s`, `2h 8m`, `4d`) computed from process start if launchctl exposes it, else `-`.
- [ ] `--json` flag emits a JSON array instead of the table (machine-readable; same fields).
- [ ] Output auto-disables Rich color/formatting when stdout is not a TTY (plain text).
- [ ] Exit 0 if all queried services are loaded **and** running; 2 if any are not loaded; 0 with non-running services still considered an informational state (user asked for status, not health).

**Edge Cases**:
- `launchctl print` returns non-zero → row shows `loaded=no`, all other fields `-`.
- Parsing fails for an individual field → field is `unknown`; CLI does not crash.

---

### 5.7 Feature: Logs (`amc logs`)

**Priority**: P0 (Critical)

#### User Stories
**US-007**: As the operator, I want to tail the application logs for one or all services with one command so that I never look up the log filename or path again.

**Acceptance Criteria**:
- [ ] `amc logs [name|all]` tail-follows the most recent matching app log file(s) in `~/Library/Logs/messaging-agent/`.
- [ ] Default app-log file selection: today's `adapter-YYYY-MM-DD.log`, `receiver-YYYY-MM-DD.log`, `backup-YYYY-MM-DD.log` based on the system clock.
- [ ] If today's file does not yet exist, fall back to the newest matching file by glob.
- [ ] `--launchd` flag tails launchd-level stdout/stderr instead (`launchd-stdout.log` / `launchd-stderr.log` for adapter; `launchd-receiver-*.log` for receiver; etc.).
- [ ] `--no-follow` prints existing content and exits.
- [ ] `-n N` (default 50) prints the last N lines before following (or before exiting if `--no-follow`).
- [ ] With `all`, output is interleaved; each line is prefixed `[<service>]`.
- [ ] Ctrl-C exits cleanly.

**Edge Cases**:
- No matching log file → exit 1 with `No logs found for <service>. Has it run yet?`.
- Log file rotates while tailing (date rolls over at midnight) → CLI keeps following the original file; new content goes to the new file. (Out-of-scope: hot-following the rotation. Operator can ^C and rerun.)

---

### 5.8 Feature: Doctor (`amc doctor`)

**Priority**: P1 (High)

#### User Stories
**US-008**: As the operator, I want a preflight diagnostic that checks every common misconfiguration so that "why isn't this working?" has a one-command answer.

**Acceptance Criteria**:
- [ ] `amc doctor` runs the following checks and prints a Rich-formatted result table with `check`, `status` (`ok` / `warn` / `fail`), `details`:
  - `uv` is on `PATH`
  - `~/.config/messaging-agent/.env` exists and is readable
  - The current process can read `~/Library/Messages/chat.db` (proxy for Full Disk Access on the adapter binary; warn if the test fails — explain that the granted FDA principal is the launchd-spawned binary, not the CLI)
  - Each registered plist template exists in `ops/launchd/`
  - Each plist is installed in `~/Library/LaunchAgents/`
  - Each service is bootstrapped (via `launchctl print`)
  - Ports 8080 and 8090 are not bound by an unrelated process
  - `~/Library/Logs/messaging-agent/` exists and is writable
- [ ] Exit 0 if all checks `ok` or `warn`; 2 if any `fail`.
- [ ] `--json` flag emits results as JSON.

**Edge Cases**:
- A check raises unexpectedly → row shows `status=fail`, `details=<exception class>: <message>`; doctor still runs remaining checks.

---

### 5.9 Feature: Top-Level UX

**Priority**: P1 (High)

#### User Stories
**US-009**: As the operator, I want `amc --help` to list every command in a discoverable hierarchy so that I don't need to consult the spec to remember the surface.

**Acceptance Criteria**:
- [ ] `amc --help` lists: `serve`, `install`, `uninstall`, `status`, `logs`, `doctor`, `service`.
- [ ] `amc service --help` lists: `start`, `stop`, `restart`, `enable`, `disable`.
- [ ] `amc --version` prints the package version from `amc/__init__.py` (or `importlib.metadata`).
- [ ] Every leaf command has a one-line description and at least one usage example.

## 6. Non-Functional Requirements

### 6.1 Performance
- CLI cold start to `--help` output: under 200 ms on a modern Mac. Avoid heavy imports at top level; lazy-import `rich` / `subprocess` helpers as needed.
- `amc status` over 3 services: under 500 ms (three `launchctl print` calls; not parallelized in v1).
- `amc logs` adds negligible overhead over `tail -F`.

### 6.2 Security
- The CLI runs as the operator's user; no privileged operations.
- No new attack surface: every shell-out passes args as `list[str]` to `subprocess.run` (no `shell=True`).
- Plist substitution accepts only `__INSTALL_DIR__` and `__HOME__`; both are derived from local paths, not user input.
- The CLI does not read or display the bearer token from `.env`.

### 6.3 Scalability
- Single-Mac, single-user. The service registry is a hard-coded list of three entries; growth is bounded by the project's launchd footprint, not user count.

### 6.4 Accessibility
- All Rich output auto-degrades to plain text in non-TTY contexts (pipes, CI logs).
- `--json` flag on `status` and `doctor` for screen readers / programmatic consumers.

## 7. Technical Considerations

### 7.1 Architecture Overview

The CLI is a thin Typer application that wraps two primitives: (1) a service registry that knows about every launchd-managed component, and (2) a launchctl helper that wraps `subprocess.run` calls. Every command is a function that takes a parsed target list (one or more services), iterates, and prints a result.

```
amc CLI (Typer app)
├── amc/cli/__init__.py         # console_scripts entry: app
├── amc/cli/app.py              # Typer app + top-level commands (serve, install, …)
├── amc/cli/services.py         # Service dataclass + registry (adapter, receiver, backup)
├── amc/cli/launchctl.py        # subprocess helpers + launchctl print parser
├── amc/cli/plist.py            # template substitution + plutil -lint
├── amc/cli/logs.py             # log path resolution + tail-follow
├── amc/cli/doctor.py           # preflight checks
└── amc/cli/output.py           # Rich table / plain / JSON output helpers
```

### 7.2 Tech Stack
- **Language**: Python 3.12 (matches root project).
- **CLI framework**: Typer (new root dependency).
- **Output**: Rich (new root dependency; auto-detect TTY, plain in pipes).
- **Process**: stdlib `subprocess`, `os.execvp` (for `serve`).
- **Tests**: `pytest`, `typer.testing.CliRunner`, `unittest.mock.patch` for subprocess.

### 7.3 Integration Points
| System | Integration Type | Purpose |
|--------|------------------|---------|
| `launchctl` (system binary) | subprocess | Service bootstrap/bootout/kickstart/enable/disable/print |
| `plutil` (system binary) | subprocess | Lint rendered plists before atomic move |
| `~/Library/LaunchAgents/*.plist` | File I/O | Render and atomically move plist files |
| `~/Library/Logs/messaging-agent/*.log` | File I/O | Resolve and tail-follow log files |
| `ops/launchd/*.plist` | File I/O | Read plist templates |
| `ops/launchd/run-*.sh` | Referenced only | Run scripts remain; CLI does not invoke them directly (launchd does) |

### 7.4 Technical Constraints
- macOS-only. The CLI imports nothing that pretends to abstract launchd.
- Single-operator. No locking, no multi-user contention.
- `launchctl print` output format is undocumented; parsers must be defensive.

### 7.5 Codebase Context

#### Existing Architecture
The AMC repository is a uv workspace with the adapter at root (`amc/`) and two workspace members (`mcp/`, `webhook-receiver/`). Three launchd services are already defined under `ops/launchd/` with plist templates, run scripts, and a bash `install.sh`. Logs are written to `~/Library/Logs/messaging-agent/` in two flavors per service: launchd-level `*-stdout.log` / `*-stderr.log` and app-level rotated `<service>-YYYY-MM-DD.log` (structured JSON from the app loggers).

#### Integration Points
| File/Module | Purpose | How This Feature Connects |
|-------------|---------|---------------------------|
| `ops/launchd/install.sh` | Current bash installer | Deleted in Phase 2; logic ported to `amc/cli/plist.py` and `amc/cli/launchctl.py` |
| `ops/launchd/*.plist` | Plist templates with `__INSTALL_DIR__` / `__HOME__` placeholders | Read by `amc install`; substitution logic ported verbatim |
| `ops/launchd/run-*.sh` | Wrapper scripts that exec uvicorn under launchd | Untouched; `amc install` continues to point plists at these |
| `amc/app.py` | Adapter FastAPI app | Targeted by `amc serve adapter` via `uv run uvicorn amc.app:app` |
| `webhook-receiver/amc_receiver/app.py` | Receiver FastAPI app | Targeted by `amc serve receiver` via `uv run --project webhook-receiver uvicorn …` |
| `pyproject.toml` (root) | Workspace + root deps | Adds `typer` and `rich` to `[project] dependencies`; adds console script entry |
| `RUNBOOK.md`, `SETUP.md`, `ops/launchd/README.md` | Operational docs | Rewritten in Phase 2 to point at `amc <cmd>` |

#### Patterns to Follow
- **Env-driven config + `from_env()` classmethod** (used in `amc/core/{auth,logging,…}.py`): the CLI does not need env-driven config in v1, but if a future change adds it, follow this pattern.
- **Module-level config cache** (`_configured_X` / `get_X` / `reset_X`): if any module needs a one-time-loaded resource, mirror this.
- **Test fakes under `tests/fakes/`**: launchctl mocks would land in `tests/fakes/launchctl.py` if reused across test modules.
- **Defensive parsing**: see `amc/connectors/imessage/reader.py::decode_attributed_body` for the established pattern of treating external binary formats as untrusted.

#### Related Features
- **MCP wrapper (`mcp/`)**: a separate Python entry point; demonstrates the workspace-member pattern. The CLI deliberately does NOT become a workspace member — it lives inside the root `amc/` package so it can `from amc.* import` without dependency gymnastics.

## 8. Scope Definition

### 8.1 In Scope
- Manage three launchd services: adapter, receiver, backup.
- Foreground manual run for adapter and receiver via `amc serve`.
- Install (render + plutil + bootstrap + enable), uninstall (bootout + optional rm), lifecycle (start/stop/restart/enable/disable).
- Status with Rich table + `--json`.
- Log tailing with app-log default and `--launchd` opt-in; `all` mode interleaves.
- `amc doctor` preflight diagnostics.
- Replace `ops/launchd/install.sh` with the CLI; update all docs to point at `amc`.

### 8.2 Out of Scope
- Configuration editing (.env management): the `.env` file remains hand-edited; rationale: deliberate keep-it-simple scope cut. Adding `--set KEY=VAL` introduces a parser, a backup story, and a validation surface for negligible gain.
- DB / migrations (`alembic upgrade`): direct `uv run alembic …` remains the workflow; rationale: operators run migrations rarely and need full alembic flags when they do.
- Cross-platform support (Linux systemd, Windows): launchd is the single supervision target.
- Pre-emptive port-conflict detection in `amc serve` (uvicorn's own error is acceptable; doctor flags it).
- Hot-following log rotation across the midnight boundary.
- Parallel `launchctl print` calls in `amc status` (sequential is fast enough at N=3).

### 8.3 Future Considerations
- `amc backup run` for one-shot backup runs (separate from the launchd schedule).
- `amc smoke` — health-check probe against `/healthz` plus a sample message round-trip.
- `amc serve all` (multiplexed uvicorn supervision under a single foreground process).
- Auto-pause logic: `amc serve adapter` could detect a running launchd service and prompt to `amc service stop adapter` first.

## 9. Implementation Plan

### 9.1 Phase 1: Foundation
**Completion Criteria**: `amc --help`, `amc status`, and `amc logs` work end-to-end against a launchd setup that was installed by the existing `install.sh`.

| Deliverable | Description | Dependencies |
|-------------|-------------|--------------|
| Typer scaffold | `amc/cli/{__init__,app}.py`; console script `amc = "amc.cli.app:app"` in `pyproject.toml`; `typer` + `rich` added to root deps | none |
| Service registry | `amc/cli/services.py` with `Service` dataclass and three registered services; `resolve_targets("adapter" \| "all" \| …)` helper | Typer scaffold |
| launchctl helpers | `amc/cli/launchctl.py` with `print_service`, `print_disabled_state`, `kickstart`, `bootout`, `enable`, `disable`, `kill_sigterm`; each returns a typed result | Service registry |
| Status command | Rich table + `--json`; parses `launchctl print` output | launchctl helpers |
| Logs command | Resolve log paths; tail-follow with prefix labels; `--launchd`, `--no-follow`, `-n` | Service registry |
| Unit tests (foundation) | `typer.testing.CliRunner` + mocked subprocess for all of the above | All Phase 1 deliverables |

**Checkpoint Gate**: Operator runs `amc status` on a real machine and confirms output matches `launchctl print` ground truth for all three services.

---

### 9.2 Phase 2: Install / Uninstall
**Completion Criteria**: `amc install` and `amc uninstall` are operational; `ops/launchd/install.sh` is deleted; all docs point at `amc`.

| Deliverable | Description | Dependencies |
|-------------|-------------|--------------|
| Plist renderer | `amc/cli/plist.py`: read template, substitute `__INSTALL_DIR__` / `__HOME__`, `plutil -lint`, atomic write | Phase 1 |
| Install command | Bootout-if-loaded → render → write → bootstrap → enable; create `~/Library/LaunchAgents/` and `~/Library/Logs/messaging-agent/` if missing | Plist renderer |
| Uninstall command | Bootout + remove plist; `--keep-plist`; tolerate already-unloaded state | Install command |
| Doc updates | Rewrite `ops/launchd/README.md`, `RUNBOOK.md`, `SETUP.md`; remove all references to `install.sh` | Install + uninstall complete |
| Delete `install.sh` | Remove `ops/launchd/install.sh` | Doc updates merged |
| Unit tests | Mock filesystem (tmp_path) + subprocess; verify substitution, lint, atomic move, idempotency | Install + uninstall |

**Checkpoint Gate**: Operator runs `amc install` on a fresh machine state and verifies all three services bootstrap and start automatically. Then `amc uninstall all` cleanly removes them.

---

### 9.3 Phase 3: Lifecycle & Manual Serve
**Completion Criteria**: Every routine ops verb is reachable as a single `amc` invocation.

| Deliverable | Description | Dependencies |
|-------------|-------------|--------------|
| `amc service` group | Typer subgroup wiring `start`, `stop`, `restart`, `enable`, `disable` to launchctl helpers; resolve targets; aggregate exit codes | Phase 1 |
| `amc serve adapter\|receiver` | `os.execvp("uv", ["uv", "run", ...])` for adapter; `["uv", "run", "--project", "webhook-receiver", ...]` for receiver; `--host` / `--port` overrides; `serve backup` rejected | Service registry |
| Lifecycle unit tests | Mock subprocess for each verb across each target; assert correct launchctl args | `amc service` group |

**Checkpoint Gate**: Operator runs through the full sequence on a real machine: `serve adapter` (Ctrl-C), `service start all`, `service restart adapter`, `service disable receiver`, `status`, `service enable receiver`, `service stop all`.

---

### 9.4 Phase 4: Doctor & Polish
**Completion Criteria**: `amc doctor` covers all listed checks; CLI is documented; tests pass.

| Deliverable | Description | Dependencies |
|-------------|-------------|--------------|
| Doctor checks | Each check is a function returning `(status, details)`; runner iterates and renders Rich table | Phases 1–3 |
| `--json` output | Status and doctor both emit JSON when flag is set | Doctor |
| README / docs section | `amc CLI` section in root README; brief reference table of every command | Doctor |
| Final test sweep | Coverage: every command, every flag, every exit-code path; ruff clean | All previous |

**Checkpoint Gate**: User-acceptance run: operator clones the repo on a clean machine, runs `amc install`, `amc doctor`, `amc service start all`, `amc status` and confirms all green. No `install.sh` invocation anywhere in the runbook.

## 10. Dependencies

### 10.1 Technical Dependencies
| Dependency | Owner | Status | Risk if Delayed |
|------------|-------|--------|------------------|
| Typer (PyPI) | Project root | New dep | None — well-maintained, BSD-licensed |
| Rich (PyPI) | Project root | New dep | None — well-maintained, MIT-licensed |
| Existing `ops/launchd/*.plist` templates | This repo | Stable | If templates change shape, registry needs update |
| `launchctl` / `plutil` / `uv` on `PATH` | macOS / user environment | Assumed | Doctor surfaces missing `uv` |

### 10.2 Cross-Team Dependencies
N/A — single-operator project.

## 11. Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation Strategy | Owner |
|------|--------|------------|---------------------|-------|
| `launchctl print` output format changes across macOS versions | Med | Med | Defensive line-by-line parsing; unknown fields fall back to `-` / `unknown`; integration smoke run on each macOS upgrade | Operator |
| Service registry drifts from `ops/launchd/*.plist` files (e.g., new plist added without registry update) | Med | Low | `amc doctor` flags missing-template and orphan-plist cases; unit test asserts every registered service has a template file on disk | Operator |
| Deleting `install.sh` breaks an unupdated doc or CI step | Low | Low | Grep audit before deletion; grep for `install.sh` in docs/CI in Phase 2 | Operator |
| `os.execvp("uv", …)` behaves differently from `uv run` directly | Low | Low | Match the exact arg vector that `install.sh` uses today; manual smoke test for both `serve` targets | Operator |
| Rich color rendering misbehaves in some terminals | Low | Low | Auto-disable on non-TTY; document `NO_COLOR` env var support (Rich respects it natively) | Operator |

## 12. Open Questions

| # | Question | Owner | Due Date | Resolution |
|---|----------|-------|----------|------------|
| 1 | Should `amc serve adapter` proactively detect a running launchd adapter and abort/prompt? | Operator | Phase 3 | Deferred to "Future Considerations"; v1 lets uvicorn's port-bind error surface |
| 2 | Should the CLI ship with shell completion (`amc --install-completion`)? | Operator | Phase 4 | Typer provides this for free; enable in Phase 4 polish |
| 3 | Should `amc logs` support absolute date selection (`--date 2026-05-09`)? | Operator | Future | Not in v1; today's file plus newest-fallback covers 95% |

## 13. Appendix

### 13.1 Glossary
| Term | Definition |
|------|------------|
| launchd | macOS service supervisor that owns user-level daemons (LaunchAgents) |
| LaunchAgent | A launchd-managed user service; defined by a plist in `~/Library/LaunchAgents/` |
| Label | The unique service identifier in a plist (`com.user.amc-adapter`) |
| Domain | The launchctl scope; here always `gui/<uid>` |
| Bootstrap / Bootout | launchctl verbs for load / unload |
| Kickstart | launchctl verb to start (or restart with `-k`) a loaded service |
| FDA | Full Disk Access — macOS permission required to read `chat.db` |

### 13.2 References
- `CLAUDE.md` — project conventions and existing patterns
- `specs/agent-messaging-channel-SPEC.md` — AMC v1 spec (source of truth for service architecture)
- `internal/blueprints/agent-messaging-channel.md` — original blueprint
- `ops/launchd/README.md` — current launchd documentation (to be rewritten)
- `RUNBOOK.md` — operational runbook (to be updated)
- `SETUP.md` — onboarding guide (to be updated)
- Typer documentation: https://typer.tiangolo.com/
- Rich documentation: https://rich.readthedocs.io/
- `launchctl(1)`, `plutil(1)` man pages

---

*Document generated by SDD Tools*
