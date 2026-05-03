# ADR 0006: MCP wrapper is stdio-only in v1

**Status**: Accepted
**Date**: 2026-05-03

## Context

The Model Context Protocol (MCP) defines two primary transports:

- **stdio** — the MCP server is spawned as a child process of the agent runtime; messages flow over the child's stdin/stdout. Each agent session gets its own short-lived server process.
- **HTTP** (Streamable HTTP / SSE) — the MCP server is a long-running daemon listening on a network port; agents connect over HTTP. One server can serve many agents.

The blueprint (§7) noted both transports as candidates and described HTTP as "long-lived MCP server bound to `localhost:8081` (or remote)" for agents that cannot or do not want to spawn a stdio child. The Phase 1 reconciliation pinned v1 at stdio-only, but did not record the *why*.

The forces:

- The named v1 client matrix is **Claude Code, Claude Desktop, Codex CLI, and Codex Desktop** (spec §5.6). All four spawn MCP servers as stdio children today; none require an HTTP-connected server.
- The MCP wrapper (blueprint §6) is a thin TypeScript layer that translates four tools into HTTP calls against the adapter. It carries no platform code and almost no state — building it as a daemon would mean adding listener, auth, session management, and lifecycle logic that the stdio model gets for free.
- An HTTP MCP server reachable on the local network is a security surface in its own right. It needs auth (presumably the same bearer token model as the adapter), needs to bind correctly (localhost vs. LAN), and needs to handle multiple concurrent agent sessions cleanly. This is real work for v1's zero current beneficiaries.
- The adapter HTTP API (spec §5.1, §7.4) already provides a network-reachable surface for non-MCP consumers. Anyone who needs a "remote-accessible MCP" today can hit the adapter directly over HTTP.

## Decision

The MCP wrapper ships **stdio-only** in v1. The HTTP MCP transport is explicitly deferred to post-v1.

Concrete consequences:

- The wrapper is published as an executable that reads/writes MCP frames on stdin/stdout (the default for `@modelcontextprotocol/sdk` stdio servers).
- Agent runtimes spawn one wrapper process per session. Process lifetime is bound to the session; no daemon mode.
- The wrapper validates Phase-3 acceptance via an automated `@modelcontextprotocol/sdk` **client** harness that spawns the wrapper as a subprocess and round-trips every tool against the adapter — no real MCP host, no MCP Inspector, and no human-driven verification (see ADR 0007 and spec §9.3).
- Configuration (`AMC_BASE_URL`, bearer token, `X-Agent-ID` to send on calls) is injected via env vars at spawn time, in keeping with how MCP hosts already configure stdio servers.
- The HTTP transport, if/when added post-v1, will be additive: same four tools, same JSON shapes, just a different transport. Spec §15 lists this under future work.

## Consequences

### Positive

- **Zero networking surface for the wrapper.** No port to bind, no CORS to configure, no LAN-vs-localhost binding decision. Less code, less config, less to misuse.
- **Per-session isolation.** Each agent gets a fresh wrapper process; bugs cannot leak state between sessions because there is no shared state.
- **No auth concerns for the wrapper.** Auth lives in the adapter (bearer token); the wrapper just relays the configured token over HTTP. The wrapper never sees an untrusted caller.
- **Fits every named v1 client.** All four target clients spawn stdio MCP servers natively, so users do not need a separate "start the MCP daemon" step in their setup.
- **Cleaner crash behavior.** A wrapper crash takes down only the affected agent session; the agent runtime spawns a fresh one on next call.
- **Smaller test matrix.** Phase 3 acceptance only has to validate one transport, against one programmatic SDK harness.

### Negative

- **Remote agents are not supported in v1.** An agent on a different machine cannot use the MCP wrapper directly — it must hit the adapter HTTP API (also localhost-bound by default; binding wider is an operator choice). For the named v1 clients this is not a constraint.
- **Each agent session pays a process spawn.** Negligible (~tens of ms in Node), but present.
- **No multi-agent fan-out from a single wrapper.** Two agents sharing a wrapper would need shared state and concurrent request handling that the stdio model doesn't naturally provide. Not a v1 use case.

### Neutral

- The four-tool surface (§6.1) is transport-agnostic. Adding HTTP later requires no API changes.
- The adapter's REST endpoints already serve non-MCP consumers, so "I want to use AMC from a Python script" is solved without ever touching MCP.

## Alternatives considered

- **stdio + HTTP from day one.** Rejected — doubles the surface to test, adds auth and binding decisions, with no v1 client that needs HTTP.
- **HTTP MCP only, no stdio.** Rejected — three of the four named v1 clients (Claude Code, Codex CLI, Codex Desktop) prefer stdio, and Claude Desktop's stdio integration is the most ergonomic by far. Forcing HTTP would make the install instructions worse for every named user.
- **stdio with an optional HTTP shim daemon outside the wrapper.** Effectively the same surface as "HTTP from day one," just with extra plumbing. Rejected as deferred future work, not v1.

## References

- Blueprint §6 — MCP Wrapper
- Blueprint §7 — Deployment on a Single Mac
- Blueprint §7.2 — Future Work: HTTP MCP transport
- Spec §5.6 / REQ-AMC-006 — MCP wrapper exposing the four tools
- Spec §8.1 / §8.2 — Out-of-scope: HTTP MCP transport for v1
- Spec §9.3 — Phase 3 automated SDK-client harness for acceptance
- ADR 0007 — Autonomous build acceptance (stdio-only is what makes the SDK-client harness sufficient)
