# Agent Messaging Gateway (AMG)

AMG is a personal-scale messaging gateway that
exposes the same four-tool MCP surface (and an equivalent REST surface) over
both iMessage and Discord, normalizing their very different I/O models behind a
single message envelope. v1 ships as a Python+FastAPI adapter on macOS,
supervised by `launchd`, with a thin Python MCP wrapper for stdio-based
clients (Claude Code, Claude Desktop, Codex CLI, Codex Desktop) and a
documented direct-HTTP path for non-MCP consumers. The system is built
decoupled-by-design so that swapping the agent framework, the MCP wrapper, or
either connector requires no changes to the other layers.

## Architecture

Three layers, each independently replaceable: agent runtimes talk to the MCP
wrapper (or directly to the adapter over HTTP), the adapter is the source of
truth and owns persistence, and one connector per platform translates raw
events into the normalized envelope.

```mermaid
flowchart TD
    subgraph agents["Agent Runtimes"]
        CC[Claude Code]:::primary
        CD[Claude Desktop]:::primary
        XC[Codex CLI]:::primary
        XD[Codex Desktop]:::primary
        CH[Custom HTTP Client]:::primary
    end

    subgraph mcp["MCP Layer (stdio)"]
        MW[MCP Wrapper<br/>Python]:::secondary
    end

    subgraph adapter["Adapter Process (FastAPI)"]
        REST["REST API<br/>(localhost:8080)"]:::secondary
        ICX[iMessage Connector]:::secondary
        DCX[Discord Connector]:::secondary
        WHQ[Webhook Queue]:::secondary
    end

    subgraph data["Local State"]
        DB[(SQLite WAL)]:::neutral
        FS["Attachments Store"]:::neutral
        CFG["~/.config/...<br/>.env + allowlist.toml"]:::neutral
        LOG["~/Library/Logs/<br/>messaging-agent/"]:::neutral
    end

    subgraph platforms["External"]
        CDB[("~/Library/Messages/<br/>chat.db (read-only)")]:::warning
        OS["osascript<br/>(Messages.app)"]:::warning
        DG["Discord Gateway WS"]:::warning
        DR["Discord REST"]:::warning
        WH["Webhook Receiver<br/>(operator-supplied)"]:::warning
    end

    CC --> MW
    CD --> MW
    XC --> MW
    XD --> MW
    MW --> REST
    CH --> REST

    REST --> DB
    REST --> FS
    REST --> WHQ
    WHQ --> WH

    ICX <--> CDB
    ICX --> OS
    ICX --> REST
    DCX <--> DG
    DCX --> DR
    DCX --> REST

    REST --> CFG
    REST --> LOG

    classDef primary fill:#dbeafe,stroke:#2563eb,color:#000
    classDef secondary fill:#f3e8ff,stroke:#7c3aed,color:#000
    classDef warning fill:#fef3c7,stroke:#d97706,color:#000
    classDef neutral fill:#f3f4f6,stroke:#6b7280,color:#000

    style agents fill:#f8fafc,stroke:#94a3b8,color:#000
    style mcp fill:#f8fafc,stroke:#94a3b8,color:#000
    style adapter fill:#f8fafc,stroke:#94a3b8,color:#000
    style data fill:#f8fafc,stroke:#94a3b8,color:#000
    style platforms fill:#f8fafc,stroke:#94a3b8,color:#000
```

## Quick Links

The full operator and reference documentation lives under [`docs/`](docs/index.md)
as a MkDocs (Material) site. Browse it on GitHub, or build/serve it locally:

```bash
uv sync --group docs
uv run mkdocs serve   # http://127.0.0.1:8000
```

### Deploying the docs

The site is published to GitHub Pages at
<https://sequenzia.github.io/amg/> by the
[`docs` workflow](.github/workflows/docs.yml). Deployment is **manual** —
it only runs when triggered, never automatically on push:

```bash
gh workflow run docs.yml --ref main
```

…or via the GitHub UI: **Actions → docs → Run workflow** (on `main`). The
workflow installs the docs toolchain (`uv sync --only-group docs`), runs the
docs linter and `mkdocs build --strict`, then publishes the built `site/` via
`actions/deploy-pages` (artifact deployment — there is no `gh-pages` branch).
Before the first run, set the repo's Pages source to **GitHub Actions**
(Settings → Pages), or once via `gh api -X POST repos/sequenzia/amg/pages -f build_type=workflow`.

Before triggering a deploy, validate locally with the same checks CI runs:

```bash
uv run python scripts/docs_lint.py   # or: make docs-lint
uv run mkdocs build --strict
```

Key entry points:

- [Setup Guide](docs/getting-started/index.md) — operator runbook: macOS
  permissions, Discord bot creation, `.env`, allowlist, `launchd` install.
- [REST API](docs/reference/rest-api.md) and [MCP Tools](docs/reference/mcp-tools.md)
  — REST endpoints and MCP tool reference, generated from the adapter's OpenAPI
  plus a hand-written MCP section.
- [Runbook](docs/operations/runbook.md) — common failures and recoveries
  (permission revoked, gateway disconnect, AppleScript failing, webhook outage,
  Mac asleep).
- [Spec](specs/agent-messaging-gateway-SPEC.md) — full technical specification
  (requirements, architecture, data models, API contracts, test plan).
- [Blueprint](internal/blueprints/agent-messaging-gateway.md) — original
  architectural blueprint; the spec extends and, in two specific places,
  supersedes it.

## Components

### Adapter (Python)

The Python adapter under `amg/` is the source of truth: it owns the SQLite
store, runs the iMessage and Discord connectors as background tasks, and
exposes the REST surface plus the outbound webhook described in spec §5 / §7.4.

### MCP Wrapper (Python)

The Python MCP wrapper lives in [`mcp/`](mcp/) alongside the adapter as a
uv workspace member. It is a thin layer that translates the four MCP tools
(`list_unread_messages`, `send_message`, `mark_read`, `get_message_context`)
into HTTP calls against the adapter — see spec §5.6 / §7.4.7.

Quick start (from the repo root):

```bash
uv sync --all-packages                     # installs adapter + wrapper
uv run --project mcp pytest                # full wrapper test suite
uv run --project mcp ruff check .          # lint
uv run --project mcp python scripts/import_audit.py
uv run --project mcp amg-mcp               # launches the stdio MCP server
```

The wrapper has zero platform-specific imports (no Discord SDK, no
AppleScript / chat.db references); the import-audit script asserts this
statically.

## amg CLI

`amg` is the operator-facing CLI shipped with the adapter. It wraps the
foreground entry points, the `launchd` plist render/install pipeline, and a
preflight diagnostics command — everything an operator needs to install,
inspect, and troubleshoot the AMG services on a single Mac. After
`uv sync --all-packages`, the `amg` entry point is on the path; run
`amg --help` for the canonical surface.

### Command reference

| Command | Description | Example |
| --- | --- | --- |
| `amg serve {adapter\|receiver}` | Run an AMG service in the foreground (`os.execvp`-handoff to `uv run uvicorn ...`). The `backup` target is launchd-only. | `amg serve adapter` |
| `amg install [name\|all]` | Render plists and bootstrap AMG launchd services. No args = install all (`adapter`, `receiver`, `backup`). | `amg install adapter receiver` |
| `amg uninstall [name\|all] [--keep-plist]` | Bootout services and remove their plists. `--keep-plist` leaves the file on disk. | `amg uninstall all --keep-plist` |
| `amg service {start\|stop\|restart\|enable\|disable} [name\|all]` | Per-service launchd lifecycle. `start` auto-bootstraps if needed; `restart` uses `kickstart -k`. | `amg service restart adapter` |
| `amg status [name\|all] [--json]` | Show launchd state (PID, last exit, uptime) as a Rich/plain table, or JSON for tooling. | `amg status --json` |
| `amg logs [name\|all] [--launchd] [--no-follow] [-n N]` | Tail-follow service logs. `--launchd` switches to launchd-level stdout/stderr. Multi-service mode prefixes each line with `[<svc>] `. | `amg logs adapter -n 100` |
| `amg doctor [--json]` | Run preflight diagnostics (spec §5.8). Exits 2 if any check is `fail`. | `amg doctor` |

Service names are `adapter`, `receiver`, `backup`, or `all`. An empty target
list is equivalent to `all`.

### Shell completion

The `amg` CLI uses Typer's built-in completion. To enable tab-completion for
commands like `amg <TAB>` and `amg service <TAB>`:

```bash
# zsh (default on macOS)
amg --install-completion zsh
exec zsh   # or open a new terminal

# bash
amg --install-completion bash

# fish
amg --install-completion fish
```

To inspect the script without installing it, use `amg --show-completion`.
Typer auto-detects the current shell via `shellingham`; in non-TTY contexts it
prints `Shell not supported.` and exits non-zero.

## Status

**Pre-v1, in active build.**

v1 acceptance is gated by an autonomous, automated test suite — not an
operator soak. The build-acceptance gate (spec §9.4) requires:

- Bounded stability run (≤ 60 minutes, 1 msg/s synthetic traffic against the
  Phase 0 fakes) completes with zero unhandled exceptions and P95 latencies
  within the §6.1 targets (receive→visible < 3 s, send→ack < 2 s).
- Webhook delivery success rate ≥ 95% on first attempt.
- Crash-and-relaunch test passes (RPO=0 inbound, no duplicate sends).
- `launchd` plist passes `plutil -lint`; backup script unit-tested.
- Documentation finalized; docs-lint passes (links resolve, code blocks parse).

Real-Mac soak by an operator is *post-handoff* and is explicitly **not** a
build-acceptance gate.

## Project Notes

Implementer and operator notes that are not part of the public spec live under
`internal/notes/`:

- [`internal/notes/phase0-findings.md`](internal/notes/phase0-findings.md) —
  what we learned while building the Phase 0 test fixtures and fakes (chat.db
  quirks, fake Discord gateway protocol coverage map, and test-ergonomics
  decisions).

## License

TBD.
