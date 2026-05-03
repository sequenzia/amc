# ADR 0007: Build acceptance is autonomous (Phase 0 fakes + bounded stability run)

**Status**: Accepted
**Date**: 2026-05-03

## Context

The original spec (v1.0) gated each phase on human-in-the-loop activities that an autonomous AI build agent cannot perform unattended:

- **Phase 0** required a real macOS spike with Full Disk Access granted, the Automation prompt accepted, and a real iMessage round-trip to a real contact.
- **Phase 1 / Phase 2 round-trip acceptance** required a real Discord bot token and a real Apple ID respectively.
- **Phase 3 (MCP wrapper) acceptance** required interactive verification with the MCP Inspector and at least one real client (Claude Code session).
- **Phase 4 acceptance** required a 7-day soak run and a "teammate cold install in 60 minutes" test.

These gates were appropriate for a human-supervised build but blocked autonomous execution: an AI agent cannot click "Allow" on a macOS Automation prompt, cannot procure a Discord bot token for a real account, and cannot run a 7-day clock during a CI execution. The repo's reality changed when the build moved to autonomous execution by Claude Code agents (per the spec v1.1 changelog dated 2026-05-03).

The constraint: build acceptance must produce verifiable evidence that the system works **without** any human-in-the-loop step, while still validating the same end-to-end behavior the human gates were checking. The v1.1 changelog records the answer; this ADR captures the structural reasoning so it survives future spec edits.

Two design assumptions enable the autonomous model:

1. **Every external dependency can be faked at the seam.** iMessage's `chat.db` is just a SQLite file (we can build a fixture). The `osascript` send is a subprocess invocation behind a `Protocol` (we can substitute a fake). Discord is a Gateway WebSocket plus a REST API (we can stand both up in-process). Once the seams exist, no real platform credential is needed during the build.
2. **Long-duration stability can be approximated with a short, intense run.** A 7-day soak validates "does it leak under steady load." A 30-minute synthetic run at 1 msg/s with mixed traffic, deliberate webhook 5xx injection, and zero unhandled exceptions in logs gives the same shape of evidence in a runtime that fits inside CI.

## Decision

Build acceptance is fully autonomous. Replace every human-in-the-loop gate with an automated equivalent rooted in the **Phase 0 fakes** and a **bounded stability run** at the end of Phase 4. Specifically:

- **Phase 0 reframed** from "spike POC" to "build the test fixtures and platform stubs that all later phases use":
  - A deterministic fixture `chat.db` (real macOS schema, hand-built `attributedBody` typedstream archive, stable seeded rows).
  - A `FakeAppleScriptSender` implementing the `AppleScriptSender` protocol — captures sends, replays canned outcomes.
  - A fake Discord gateway WebSocket server using `websockets.asyncio.server`, plus a fake Discord REST shim patched into `discord.http.HTTPClient.request`.
  - A Phase-0 acceptance gate that runs cross-fixture smoke tests against all of the above.
- **Phase 1, 2, 3 round-trip acceptance** runs entirely against Phase 0 fakes:
  - Phase 1 — Discord round-trip: gateway → adapter → webhook → `/messages/send` → fake REST records outbound. No real bot token.
  - Phase 2 — iMessage round-trip: writer task appends to a writable copy of fixture `chat.db` → poller → adapter → `/messages/send` → `FakeAppleScriptSender` records outbound. No real Apple ID, no Full Disk Access.
  - Phase 3 — MCP wrapper round-trip: programmatic `@modelcontextprotocol/sdk` **client** harness spawns the wrapper as a stdio subprocess and exercises every tool against the adapter (which is itself wired to the Phase 0 fakes). No MCP Inspector, no Claude Code session.
- **Phase 4 acceptance** is a **bounded automated stability run**: a pytest-driven harness that boots the full stack against the Phase 0 fakes, drives 1 msg/s of mixed Discord + iMessage inbound for the configured duration (default 30 min, hard cap 60 min), randomly toggles the webhook receiver between 200 and 500 to exercise retry, and emits a JSON metrics summary asserted against §3.2 / §6.1 SLOs (P95 < 3 s receive→visible, P95 < 2 s send→ack, ≥ 95% first-attempt webhook success, zero unhandled exceptions in logs).
- **All four ADR-mandated platform prerequisites — FDA, Automation prompt, Discord bot token, real Apple ID — are reframed as deployment-time prerequisites** documented in the runbook, **not** build-time dependencies. The build never asks for them.
- **Real-Mac soak by an operator is post-handoff** and is explicitly **not** a build-acceptance gate.

## Consequences

### Positive

- **The full build runs unattended.** No "wait for the human to grant Full Disk Access" pause, no "wait for the human to enter a Discord token" pause. CI can drive every gate.
- **Reproducibility.** Phase 0 fixtures are deterministic (frozen `chat.db` builder, stable seeded rows, fixed UUIDs/ULIDs/timestamps) so failures are diagnosable from logs alone.
- **No real platform side effects during build.** Nothing the build does sends a real iMessage or a real Discord message — safe to run as often as desired.
- **Crash + relaunch path is exercised.** The bounded stability run includes a forced restart and verifies `connector_state` resumes correctly. Previously this was implicit in the 7-day soak.
- **The same fakes are reusable for development.** Anyone working on AMC can develop and test locally without touching real Discord or iMessage.

### Negative

- **The build doesn't validate the macOS permission flow.** First-time AppleScript send still triggers an Automation prompt at deploy time, and missing Full Disk Access still produces a confusing failure. Mitigated by the deployment-time runbook and explicit pre-flight checks at adapter start.
- **Real CDN expiry and real Apple ID quirks aren't covered.** A Discord CDN URL that expires after 24 h, or an Apple ID that throttles iMessage send, won't be observed in the build. Acceptable risk: the seams are clean, the deploy-time runbook documents these prerequisites, and operator monitoring is post-handoff tooling per §11.3.
- **Soak-duration leaks may slip through.** A leak that manifests after 24 h won't show in a 30-min run. Documented as known limitation; operator soak is post-handoff.
- **Cap of 60 min is arbitrary.** Long enough to detect obvious leaks and accumulator bugs, short enough to fit in CI. Picked to match the §3.2 SLO budget.

### Neutral

- The fakes themselves become a maintained surface that must track real-platform behavior changes. In practice both Discord's wire format and `chat.db`'s schema evolve slowly; the cost is modest.
- Test counts grow (Phase 0 alone added 49 tests; the bounded stability harness will add more). This is a feature, not a cost, given the goal.

## Alternatives considered

- **Keep the human-in-the-loop gates and accept that build is not autonomous.** Rejected — the entire point of the v1.1 changelog was to remove this constraint so AI agents can build the system end-to-end.
- **Keep the 7-day soak but mock time.** Rejected — most leak categories worth catching (FD exhaustion, accumulator overflow, sweep-misses-clock-drift) need real wall-clock duration, not mocked time. A real-but-shorter run is more honest.
- **Real Discord with a throwaway test bot.** Rejected — token management for a build-time dependency is operationally fragile and produces false negatives when Discord rate-limits or has an outage. The fake gateway + fake REST shim covers the same wire shapes deterministically.
- **Real iMessage with a dedicated test Apple ID.** Rejected — same fragility, plus requires a physical Mac with Messages signed in for every CI run.

## References

- Spec §15.4 v1.1 Change Log entry (2026-05-03) — the canonical record of this shift
- Spec §9.0 — Phase 0 fixtures and platform stubs
- Spec §9.1 / §9.2 / §9.3 / §9.4 — phase-by-phase acceptance criteria all rooted in fakes
- Spec §3.2, §6.1 — SLOs validated by the bounded stability run
- Spec §10.3 — Bounded stability run definition
- ADR 0001 — Python + FastAPI (enables the in-process fakes via `asyncio` + `websockets`)
- ADR 0006 — MCP stdio-only (makes Phase-3 acceptance reducible to a subprocess SDK harness)
- `internal/notes/phase0-findings.md` — Phase 0 acceptance gate result
