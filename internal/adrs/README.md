# Architecture Decision Records

This directory holds the Architecture Decision Records (ADRs) for the Agent Messaging Channel (AMC). Each ADR captures a single architectural decision in the **Context / Decision / Consequences** format.

ADRs are immutable once accepted. If a decision is reversed, add a new ADR that supersedes the old one and update the `Status` line on the superseded record.

## Format

Each ADR follows the same structure:

- **Title** — short imperative sentence describing the decision.
- **Status** — `Proposed` | `Accepted` | `Superseded by NNNN`.
- **Date** — ISO date of acceptance.
- **Context** — what forces are at play; why is a decision needed.
- **Decision** — what we are doing.
- **Consequences** — what follows from this decision (positive, negative, neutral).
- **Alternatives considered** — options weighed and rejected, with the reason.
- **References** — spec sections, blueprint sections, related ADRs, source links.

## Index

| #    | Title                                                            | Status   | Date       |
|------|------------------------------------------------------------------|----------|------------|
| [0001](0001-language-pick.md) | Adapter language: Python + FastAPI         | Accepted | 2026-05-03 |
| [0002](0002-per-agent-cursor.md) | Per-agent read state via `message_reads` join table | Accepted | 2026-05-03 |
| [0003](0003-attachment-rehost.md) | Attachments are re-hosted by the adapter   | Accepted | 2026-05-03 |
| [0004](0004-idempotency-keys.md) | Client-supplied Idempotency-Key with 24 h cache and body-hash collision detection | Accepted | 2026-05-03 |
| [0005](0005-allowlist-toml.md) | Sender allowlist as a TOML file with cross-platform `person_id` linking | Accepted | 2026-05-03 |
| [0006](0006-mcp-stdio-only.md) | MCP wrapper is stdio-only in v1                   | Accepted | 2026-05-03 |
| [0007](0007-autonomous-build-acceptance.md) | Build acceptance is autonomous (Phase 0 fakes + bounded stability run) | Accepted | 2026-05-03 |

## Cross-references

- Source-of-truth blueprint: [`internal/blueprints/agent-messaging-channel.md`](../blueprints/agent-messaging-channel.md)
- Detailed specification: [`specs/agent-messaging-channel-SPEC.md`](../../specs/agent-messaging-channel-SPEC.md)
- Resolved open questions: [`internal/notes/`](../notes/)
