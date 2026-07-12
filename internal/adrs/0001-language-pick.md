# ADR 0001: Adapter language is Python + FastAPI

**Status**: Accepted
**Date**: 2026-05-03

## Context

The blueprint (§5) intentionally left the adapter language as an open architectural choice between two finalists:

- **Python + FastAPI** — strong stdlib for SQLite and macOS interop, well-documented async story, Pydantic for envelope validation.
- **TypeScript + Hono** — shared runtime with the MCP wrapper, small footprint, modern type system.

Both can implement the REST surface in §5.1 and the storage schema in §5.3. The choice has long-term consequences: it determines which language hosts both connectors, the test harness, the migration tooling, and the deployment unit. The MCP wrapper is **fixed at TypeScript** regardless of this decision (per §6 of the blueprint), so there is no language-unification benefit available — exactly one of the two layers will be polyglot either way.

Specific forces at play:

- The iMessage connector polls `~/Library/Messages/chat.db` (a local SQLite file) and shells out to `osascript`. Python's stdlib `sqlite3` covers the read path with no dependency, and `subprocess` covers the AppleScript invocation. TypeScript would pull in `better-sqlite3` (native build) plus `node:child_process`.
- The Discord connector needs a Gateway WebSocket plus REST. Both `discord.py` and `discord.js` are mature; neither is a deciding factor.
- The user's global standards (`~/.claude/CLAUDE.md`) explicitly designate Python tooling: `ruff`, `uv`, `pytest`, type hints. There is no equivalent prescription for TypeScript.
- Spec §6.1 of the spec already records the language pick as Python+FastAPI, and Phase 1 implementation (tasks #14, #20, etc.) has shipped against it — `pyproject.toml`, `uv.lock`, and 168 passing tests now exist on this assumption.

## Decision

The adapter is implemented in **Python 3.12+ with FastAPI**, packaged with `uv`, linted with `ruff`, tested with `pytest` + `pytest-asyncio`. The MCP wrapper remains TypeScript (per blueprint §6, unchanged).

Concrete consequences of this choice already locked in by Phase 1:

- Top-level `amg/` package layout (not `src/amg/`), with the `uv_build` backend.
- `tomllib` (stdlib, 3.12+) for parsing the allowlist file (see ADR 0005).
- `aiosqlite` + `sqlalchemy[asyncio]` for the writable adapter DB; `sqlite3` (stdlib) in `to_thread` for the read-only `chat.db` path.
- `discord.py` (PyPI: `discord-py`, imports as `discord`) for the Gateway and REST client.
- `structlog` for JSON logging; `pydantic` v2 for envelope validation; `alembic` for migrations.

## Consequences

### Positive

- Aligns with the user's mandated tooling, lowering friction on every future change.
- Stdlib `sqlite3` and `subprocess` keep the iMessage path dependency-light — the connector can read `chat.db` and shell out to AppleScript with zero third-party packages.
- Pydantic v2 covers the envelope contract (§3) cleanly; `@field_serializer` and `Annotated` string types map directly onto the spec's wire shape.
- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`) handles the async-heavy test surface (Discord gateway fakes, chat.db writers, webhook receivers) without ceremony.
- Python typing is sufficient for this codebase; `typing.Protocol` covers the structural-typing needs (e.g., `AppleScriptSender`).

### Negative

- The codebase is polyglot (Python adapter + TypeScript MCP wrapper). Two linters, two package managers, two test runners. This was unavoidable: the MCP SDK choice is already TypeScript.
- `discord.py` uses `aiohttp` internally rather than `httpx`, so `respx` (the chosen HTTP mocking library) cannot intercept it — Phase 0 had to ship a separate fake Discord REST shim. See `tests/fakes/discord_rest.py`.
- macOS-only async file watching is awkward; we use stdlib `signal` + 1 s polling instead of `watchdog`, trading freshness budget for no native dependency. P95 < 3 s receive→visible (spec §6.1) still fits inside this budget.

### Neutral

- The adapter binds to localhost (§5.2) so single-language performance differences (Python vs Node startup, request throughput) are not a constraint at v1 scale.
- Future swap to TypeScript (e.g., to consolidate with the MCP wrapper) is technically possible because the wire contracts (§3 envelope, §5.1 endpoints) are language-agnostic. The cost is a full rewrite of the adapter; no existing layer would be reused.

## Alternatives considered

- **TypeScript + Hono for the adapter.** Rejected because (a) it would pull `better-sqlite3` native dependencies, (b) it duplicates none of the MCP wrapper code in practice (the wrapper is just four HTTP calls), and (c) it conflicts with the user's explicit Python-tooling mandate.
- **Python + Flask** instead of FastAPI. Rejected because Flask's async story is bolted on; FastAPI's async-native + Pydantic integration is a better fit for the envelope-heavy contract surface.
- **Rust + Axum.** Considered briefly for stability under a long stability run. Rejected as overkill for a personal-scale single-Mac service; the team-of-one author would carry every dependency upgrade.

## References

- Blueprint §5 — Adapter HTTP API
- Blueprint §6 — MCP Wrapper (TypeScript pinned independently)
- Spec §6.1 — Architecture summary recording the language pick
- Global instructions: `~/.claude/CLAUDE.md` — Python tooling mandate
- Phase 1 task records: `.claude/sessions/__live_session__/execution_context.md`
