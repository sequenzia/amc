# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository State

**v1 implementation complete (2026-05-03; MCP wrapper ported from TypeScript to Python 2026-05-09; Typer `amc` CLI replaced `ops/launchd/install.sh` 2026-05-11).** Full Python adapter (FastAPI + SQLAlchemy + Alembic) + iMessage and Discord connectors + Python MCP wrapper (uv workspace member at `mcp/`) + `amc` operator CLI (`amc/cli/`, Typer + Rich) + tests + docs. The spec (`specs/agent-messaging-channel-SPEC.md`) is the source of truth for v1; the CLI spec is `specs/amc-cli-SPEC.md`; the blueprint (`internal/blueprints/agent-messaging-channel.md`) has been reconciled twice.

### Build / lint / test commands

**Adapter (root):**
- Install all (incl. wrapper): `uv sync --all-packages`
- Run app: `uv run uvicorn amc.app:app --host 127.0.0.1 --port 8080`
- Migrate: `uv run alembic upgrade head`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Test: `uv run pytest` (full suite); `uv run pytest tests/{unit-path}` for scoped
- Stability: `uv run pytest tests/stability/test_stability_run.py` (default 60s; `AMC_STABILITY_DURATION_SECONDS=1800` for 30 min)
- Docs lint: `make docs-lint` (or `uv run python scripts/docs_lint.py`)
- Docs site: MkDocs Material under `docs/` (config `mkdocs.yml`). Install deps `uv sync --group docs`; preview `uv run mkdocs serve`; validate `uv run mkdocs build --strict`. The former root docs were migrated into the site (2026-05-31): `SETUP.md`→`docs/getting-started/index.md`, `RUNBOOK.md`→`docs/operations/runbook.md`, `docs/API.md`→`docs/reference/{rest-api,mcp-tools}.md`. `docs_lint.py` now globs `docs/**/*.md` so every site page is gated. Note: MkDocs `toc` and `docs_lint` slugify `&`/`+` in headings differently — avoid those chars in headings (use "and").

**MCP wrapper (`mcp/`):**
- Run: `uv run --project mcp amc-mcp` (stdio MCP server)
- Test: `uv run --project mcp pytest`
- Lint: `uv run --project mcp ruff check . && uv run --project mcp ruff format --check .`
- Import audit: `uv run --project mcp python scripts/import_audit.py` (no platform-specific imports)

**Webhook receiver (`webhook-receiver/`):**
- Run: `uv run --project webhook-receiver uvicorn amc_receiver.app:app --host 127.0.0.1 --port 8090`
- Test: `uv run --project webhook-receiver pytest`
- Lint: `uv run --project webhook-receiver ruff check . && uv run --project webhook-receiver ruff format --check .`
- Bridges adapter outbound webhooks to one-shot `claude -p` invocations; see `webhook-receiver/README.md`.

**`amc` operator CLI (`amc/cli/`):**
- `amc serve {adapter|receiver}` — manual foreground run (uses `os.execvp` for flat process tree).
- `amc install [name|all]` / `amc uninstall [name|all] [--keep-plist]` — render plists + bootstrap/bootout launchd services.
- `amc service {start|stop|restart|enable|disable} [name|all]` — lifecycle, auto-bootstraps on `start` when needed.
- `amc status [name|all] [--json]` / `amc logs [name|all] [--launchd] [--no-follow] [-n N]` / `amc doctor [--json]` — observe.
- Cold-start budget: <200 ms. CLI modules MUST be import-light; defer heavy deps (FastAPI, SQLAlchemy, Rich tables, our own helper modules) to inside command bodies. `amc.cli.app` does NOT preload `amc.cli.{logs,serve,plist,output,launchctl,status,install,uninstall,service,doctor}` at module level.
- Test home: `tests/cli/` — `typer.testing.CliRunner` + `_FakeTTY(StringIO)` helper for Rich-path tests. Do NOT pass `mix_stderr` to CliRunner (current click 8.3.3 already separates stderr).
- Subprocess seam pattern: shell-out modules expose module-private `_run(argv)`; tests patch the attribute (e.g., `amc.cli.launchctl._run`).

### Critical domain knowledge baked into the codebase

- **iMessage `attributedBody`** is an Apple typedstream archive (NOT NSKeyedArchiver/bplist). Magic: `\x04\x0Bstreamtyped`. Decoder lives in `amc/connectors/imessage/reader.py::decode_attributed_body`.
- **`message.date` in chat.db** is mach absolute time (ns since 2001-01-01 UTC).
- **`discord.py` uses aiohttp internally** — `respx` cannot intercept it. Patch `discord.http.HTTPClient.request` (closure, not bound method).
- **`websockets` 16.0** API lives in `websockets.asyncio.server` / `websockets.asyncio.client`.
- **SQLite + Alembic gotcha**: per-connection PRAGMAs MUST go in a `connect`-event listener on the engine, NOT via `connection.exec_driver_sql()` after `connect()` (silently corrupts alembic stamp commit).
- **AppleScript injection-safe pattern**: feed the script body via stdin (`osascript -`); pass user-supplied chat_guid/text/attachment_path as positional argv to an `on run argv` handler.
- **SQLite TEXT-column ordering hazard**: emit fixed-width 6-digit microseconds in ISO 8601 strings (`'.' < 'Z'`).
- **Alembic `env.py`** calls `logging.config.fileConfig(...)` which disables existing loggers — tests must re-enable target loggers explicitly.

### Reusable patterns established
- **Env-driven config**: `ENV_*` constants + typed helpers + `from_env()` classmethod + module-specific `*ConfigError`. Examples: `amc/core/{auth, logging, rate_limit, webhook, idempotency, sweepers}.py`.
- **Time injection**: `time_provider: Callable[[], datetime] | None` kwarg defaulting to a real clock; tests pass a `_FakeClock`. Used everywhere.
- **Module-level config cache**: `_configured_X: T | None` set once at startup via `load_X()`, accessed via `get_X()`, `reset_X()` for tests.
- **Test doubles** under `tests/fakes/{name}.py`; tests colocated as `tests/fakes/test_{name}.py`.
- **MessageSink chokepoint** (`amc/core/message_sink.py`): single-transaction INSERT path that handles UPSERT senders/channels/attachments + INSERT messages + cursor advance + optional webhook enqueue. All connectors call `sink.record_inbound(envelope, source)`.
- **Pydantic v2 RFC 3339**: implement Z suffix via `@field_serializer` doing `.isoformat().replace("+00:00", "Z")` — Pydantic doesn't canonicalize tz on validate.
- **`enum.StrEnum`** instead of `class Foo(str, Enum)` (ruff `UP042`).

### Spec ↔ code divergences flagged
- `VALIDATION_FAILED` (in code) vs `VALIDATION_ERROR` (spec §7.4.12).
- DB path default: `state.db` (in code) vs `amc.db` (spec §11.2).

Tracked in `internal/notes/spec-code-divergences.md`. Resolve in a future spec revision.

## Project: Agent Messaging Channel (AMC)

AMC is a single-Mac service that lets one AI agent send and receive messages on **iMessage** and **Discord** through a unified interface. The design priority is decoupling: the agent framework, the transport, and the platform connectors must each be replaceable without rewriting the others.

## Architecture (from the blueprint)

Three layers, each independently replaceable:

```
Agent  ──MCP──►  MCP Wrapper  ──HTTP──►  Adapter HTTP API  ──►  Connectors  ──►  iMessage / Discord
   │                                          ▲
   └──────────── Direct HTTP ─────────────────┘
                                              │
                                              ▼
                                          SQLite
```

* **Adapter HTTP API** — the source of truth. A single process (FastAPI or Hono) that runs both connectors as background tasks, persists to SQLite, and exposes REST endpoints plus an outbound webhook for new messages. Agents that don't speak MCP hit this directly.
* **MCP Wrapper** — a thin Python layer using the official `mcp` SDK (FastMCP) that translates four tools (`list_unread_messages`, `send_message`, `mark_read`, `get_message_context`) into HTTP calls against the adapter. Lives at `mcp/` as a uv workspace member; must contain **no platform-specific code** (statically enforced by `mcp/scripts/import_audit.py`).
* **Connectors** — one per platform. iMessage polls `~/Library/Messages/chat.db` and sends via AppleScript; Discord uses a Gateway WebSocket plus REST.

### Critical Contracts

These shapes are what makes the layering work. Don't change them without updating the blueprint and asking the user.

* **Normalized message envelope** (blueprint §3) — every message in the system, inbound or outbound, conforms to this JSON shape. Adding a new platform means writing one connector that produces this envelope; nothing else changes.
* **MCP tool surface** (blueprint §6.1) — exactly four tools, mirroring how a human assistant works: see what's new, look up context, reply, mark done. New capabilities should be additive tools, not modifications to these four.
* **Adapter REST endpoints** (blueprint §5.1) — `/messages/unread`, `/messages/{id}`, `/messages/context`, `/messages/mark_read`, `/messages/send`, `/typing`, plus an outbound webhook.

### Storage

SQLite, four tables: `messages`, `channels`, `senders`, `identity_links` (blueprint §5.3). The `identity_links` table is what eventually maps the same human across iMessage and Discord — keep this in mind when modeling sender IDs.

## macOS-Specific Constraints (iMessage path)

These will bite a future implementer:

* The adapter process needs **Full Disk Access** granted in System Settings to read `chat.db`.
* First outbound message triggers an **Automation permission** prompt for Messages; the AppleScript send will silently fail until accepted.
* The Mac must stay awake (`caffeinate -dimsu` or Energy settings) or the connector stops processing.
* Track the last processed `ROWID` in connector state so polling survives restarts. Polling at 1s is fine — `chat.db` is local SQLite.

## Implementation Phases (planned order)

Per blueprint §8, work proceeds Discord-first because it's exercisable end-to-end with a bot token and no macOS permission dance:

1. Adapter skeleton + storage + Discord connector. Validate with `curl`.
2. iMessage connector (lift from `anthropics/claude-plugins-official/tree/main/external_plugins/imessage`, strip the MCP scaffolding, adapt to the envelope).
3. MCP wrapper — four thin HTTP calls. Verify with the MCP Inspector.
4. Hardening: webhook retries, send rate limiting, attachment re-hosting, identity linking, observability.

## Open Decisions

The blueprint intentionally leaves these for the user to call:

* **Adapter language**: settled — Python + FastAPI for the adapter; the MCP wrapper is also Python (FastMCP) since the 2026-05-09 conversion.
* **Group chat support on iMessage** in v1.
* **Attachment strategy**: pass-through vs. adapter-rehosted (Discord CDN URLs expire; iMessage attachments are local file paths — neither is a stable URL the agent can hand back).
* **Multi-agent contention** on a single adapter (per-agent cursors vs. leasing). Single-agent is the v1 default.

See blueprint §9 for the full list.

## Reference Code (external)

* iMessage connector starting point — `https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/imessage` (lift platform code, drop the MCP scaffolding)
* MCP Python SDK — `https://github.com/modelcontextprotocol/python-sdk`
* `discord.js` — recommended over the Anthropic Discord plugin for the connector

## Working Conventions for This Repo

* When the blueprint and an in-flight discussion conflict, surface the conflict and ask. Don't silently diverge from the blueprint — update it explicitly so future sessions stay aligned.
* Architectural decisions (language pick, persistence engine, package manager, deployment shape) need confirmation before code lands.
* Update this file as soon as the first runtime is scaffolded — replace the "Pre-implementation" section with real build/test/run commands.
