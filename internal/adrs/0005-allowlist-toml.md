# ADR 0005: Sender allowlist as a TOML file with cross-platform `person_id` linking

**Status**: Accepted
**Date**: 2026-05-03

## Context

REQ-AMC-007 (spec §5.7) requires that inbound messages from non-allowlisted senders are stored but **never** surfaced to the agent — they go to a quarantine table reachable only via `/messages/quarantine`. To enforce this the adapter needs a source-of-truth list of allowed senders, looked up at message-ingest time on both platforms.

Format options considered:

- **Database table** (`senders` with `allowlist_status='allowed'` rows). Available out of the box, but editing requires a SQL client or a CRUD UI. Adding a new contact at 11pm on a phone is awkward.
- **JSON file**. Editable in any text editor, but JSON is hostile to humans (no comments, trailing-comma errors, quote nesting).
- **YAML file**. Comments and readability are good, but the indentation-sensitive parser produces opaque errors on small mistakes; YAML 1.1 vs 1.2 ambiguity bites in subtle ways (the famous Norway-as-boolean problem).
- **TOML file**. Comments, no indentation rules, explicit string typing, well-defined arrays-of-tables, parsed by stdlib `tomllib` in Python 3.11+ (no dependency).

A second concern is **identity linking**: the same human reaches us on both platforms. Without a shared identity field, "Alice on iMessage" and "Alice on Discord" are two unrelated allowlist entries, and downstream agents cannot reason about "all of Alice's messages." The blueprint (§5.3) introduced `person_id` as the optional identity-link key on the `senders` table; the allowlist must be where that key is bound to the platform-specific sender IDs.

A third concern is **reload semantics**. The adapter is long-running; the allowlist file must be editable without restart. Options: file-watch (polling or `kqueue`/`fsevents`), explicit signal (`SIGHUP`), or restart-only. The spec settled on `SIGHUP` (§5.7) — explicit, no extra dependency, no debounce concerns.

## Decision

The allowlist is a **TOML file at `~/.config/messaging-agent/allowlist.toml`** (override via `AMC_ALLOWLIST_PATH`), with the following shape:

```toml
# Allowlist for the Agent Messaging Channel.
# Each entry under [[person]] binds an optional shared person_id to one or more
# platform-specific sender IDs. Messages from any sender not listed here are
# stored in the quarantine table and never reach the agent.

[[person]]
person_id  = "alice"
display_name = "Alice"
imessage   = ["+15551234567", "alice@example.com"]
discord    = ["discord:user:123456789012345678"]

[[person]]
# person_id is optional; omit when no cross-platform linking is desired.
display_name = "Bob"
imessage   = ["+15557654321"]

[[person]]
# A discord-only contact, no person_id.
display_name = "Carol"
discord      = ["discord:user:987654321098765432"]
```

Implementation rules:

- Parse with stdlib `tomllib` (Python 3.12 is already pinned per ADR 0001). No third-party dependency.
- Resolve each inbound message **once at INSERT time**: look up `(source, sender_id)`, persist `display_name`, `person_id`, and `allowlist_status` onto the `messages` and `senders` rows. Subsequent allowlist edits do **not** retroactively rewrite past rows (per spec §5.7 acceptance criteria; OQ-4 covers the related "should quarantined messages migrate when status flips" question separately).
- `SIGHUP` reloads the file. In-flight messages use the version captured at message time; the next message after reload uses the new version.
- A malformed file (TOML parse error, unknown top-level key) on startup raises `AllowlistConfigError` and the adapter refuses to start. A malformed file on `SIGHUP` is logged at ERROR; the in-memory copy is retained until the file is fixed and another `SIGHUP` is sent — the adapter does **not** silently fall back to "deny all" (which would mass-quarantine), and does not silently fall back to the on-disk file (which is broken).
- The file must be mode `0600` and owned by the running user. A more permissive mode is logged at WARN at startup.

## Consequences

### Positive

- **Trivially editable.** Add a contact in any text editor. No UI, no DB shell, no admin panel.
- **No new dependency.** `tomllib` is stdlib in Python 3.11+; the project already pins 3.12.
- **Comments survive.** TOML supports `#` comments natively, so the file documents itself.
- **Cross-platform identity built in.** `person_id` is optional but standard; when present, the storage layer's `identity_links` table is populated from the same source of truth that drives allowlist enforcement.
- **Reload is explicit and bounded.** `SIGHUP` is one signal, easy to script (`kill -HUP $(pgrep -f amc.adapter)`), no file-watch race conditions.
- **Operator-friendly co-location.** The file sits next to the secrets file (`~/.config/messaging-agent/.env`) — one config directory to back up, one to chmod 0700.

### Negative

- **No GUI.** A non-technical operator cannot edit the file from the menu bar. Acceptable for v1 (single-user, technical operator); a graphical front-end is post-v1 if desired.
- **Reload requires explicit signal.** An operator who edits the file and forgets to `SIGHUP` will be confused when new contacts still go to quarantine. Documented in the runbook.
- **TOML's array-of-tables syntax (`[[person]]`) is unfamiliar** to readers used to JSON or YAML. The example header in the file mitigates this.
- **The format conflates allowlist membership with identity linking.** A `[[person]]` entry without any platform IDs is meaningless; one without a `person_id` works fine but cannot participate in identity linking. Acceptable: the spec makes `person_id` explicitly optional.

### Neutral

- The "deny by default" posture (anyone not listed is quarantined) is the strongest safe default and matches how iMessage/Discord users actually onboard contacts (one at a time, deliberately).
- Sender IDs use the same string format as the rest of the system (E.164 for iMessage, `discord:user:<snowflake>` for Discord, per envelope §3). No translation needed at lookup.

## Alternatives considered

- **Database table for the allowlist.** Rejected — editing UX is bad; no editor support, no comments, requires either a CRUD UI or `sqlite3` shell familiarity.
- **JSON file.** Rejected — no comments, fragile to trailing commas, hostile to mixed-IP-typed fields.
- **YAML file.** Rejected — indentation-sensitive parsers produce opaque error messages on small mistakes; the YAML 1.1/1.2 ambiguities bite at exactly the wrong moments.
- **Per-sender allowlist rows in `senders` table managed by a CLI.** Functionally equivalent to "TOML + sync-on-startup," but adds a CLI surface and a sync step that can drift. Rejected as more moving parts for no win.
- **File-watch (`watchdog` / `fsevents`) instead of `SIGHUP`.** Rejected — adds a dependency, debounce semantics to design, and a race window where the file is half-written when the watcher fires.
- **Embedded `person_id` in a separate `identity_links.toml`.** Rejected — splitting the source of truth across two files makes "is X allowed and who are they?" a two-file lookup. One file is simpler.

## References

- Blueprint §5.3 — `senders.person_id`, `identity_links` table
- Spec §5.7 / REQ-AMC-007 — Sender allowlist & quarantine feature
- Spec §11.2 — `AMC_ALLOWLIST_PATH` env var
- Spec §14 OQ-4 — Migration of quarantined messages when allowlist flips (separate, still-open)
- ADR 0001 — Python + FastAPI (enables stdlib `tomllib`)
