# RUNBOOK.md — Agent Messaging Gateway (AMG)

Operator runbook for diagnosing and recovering from common failure modes.
Each scenario follows the same shape: **Symptom → Diagnose → Recover → Confirm**.

This document covers spec §11.4 ("Runbook") and the post-handoff operator
mitigations called out in §13 (Risks R-1, R-3, R-5, R-8, R-10).

> Defaults referenced below (override in `~/.config/messaging-agent/.env`):
> - `AMG_DB_PATH` = `~/Library/Application Support/messaging-agent/amg.db`
> - `AMG_ATTACHMENT_DIR` = `~/Library/Application Support/messaging-agent/attachments`
> - `AMG_LOG_DIR` = `~/Library/Logs/messaging-agent`
> - `AMG_ALLOWLIST_PATH` = `~/.config/messaging-agent/allowlist.toml`
> - `AMG_BIND_HOST:AMG_BIND_PORT` = `127.0.0.1:8080`
>
> Where the runbook says `$DB`, `$LOG_DIR`, etc., it means the resolved
> value of that env var.

## Table of Contents

1. [Adapter won't start](#1-adapter-wont-start)
2. [Discord disconnects repeatedly](#2-discord-disconnects-repeatedly)
3. [iMessage stops receiving](#3-imessage-stops-receiving)
4. [AppleScript send always fails](#4-applescript-send-always-fails)
5. [Webhook dead-lettering all messages](#5-webhook-dead-lettering-all-messages)
6. [Disk filling from attachments](#6-disk-filling-from-attachments)
7. [Migration failed](#7-migration-failed)
8. [chat.db schema changed (future macOS release)](#8-chatdb-schema-changed-future-macos-release)
9. [Identity-link split-brain](#9-identity-link-split-brain)
10. [Quick reference: log and DB queries](#quick-reference-log-and-db-queries)

---

## 1. Adapter won't start

**Symptom**: `amg status adapter` shows `state=stopped` with a non-zero
`last_exit`, or the adapter exits immediately when launched manually with
`amg serve adapter`. The HTTP port is not bound, and
`curl http://127.0.0.1:8080/healthz` fails with `Connection refused`.

### Diagnose

```bash
# Run the bundled diagnostics first — covers FDA, .env, allowlist, service state.
amg doctor

# Show the structured adapter log (last 200 lines, no follow).
amg logs adapter --no-follow -n 200

# Show the launchd-level spawn output (process-level crashes go here).
amg logs adapter --launchd --no-follow -n 200

# Filter for startup errors specifically.
amg logs adapter --no-follow -n 500 | grep -E 'level=(error|critical)' | tail -n 50
```

Walk these checks in order — they are the same checks the adapter performs at
startup, just done by hand:

```bash
# 1. FDA: can the adapter user even read chat.db? (For this to be valid, run
#    the test from the adapter's actual binary path, not Terminal.)
ls -l ~/Library/Messages/chat.db && \
  sqlite3 "file:$HOME/Library/Messages/chat.db?mode=ro" \
    "PRAGMA query_only = ON; SELECT COUNT(*) FROM message LIMIT 1;"
# Expected: a number. "operation not permitted" → FDA missing.

# 2. .env exists and AMG_BEARER_TOKEN is set + non-empty.
test -r ~/.config/messaging-agent/.env && echo "ok: .env readable" || echo "MISSING .env"
grep -E '^AMG_BEARER_TOKEN=.+' ~/.config/messaging-agent/.env >/dev/null \
  && echo "ok: token set" || echo "MISSING AMG_BEARER_TOKEN"

# 3. Allowlist file exists.
test -r ~/.config/messaging-agent/allowlist.toml \
  && echo "ok: allowlist readable" \
  || echo "MISSING allowlist.toml"

# 4. .env file mode is 600 (token is a secret).
stat -f '%A %N' ~/.config/messaging-agent/.env
# Expected: 600
```

### Recover

| If diagnosis showed | Do this |
|---|---|
| `operation not permitted` on `chat.db` | Grant **Full Disk Access** to the adapter binary in System Settings → Privacy & Security → Full Disk Access. See the [Setup Guide](../getting-started/index.md#61-full-disk-access-fda). After granting, fully restart the adapter (TCC reads at process start). |
| `MISSING .env` | `mkdir -p ~/.config/messaging-agent && cp .env.example ~/.config/messaging-agent/.env && chmod 600 ~/.config/messaging-agent/.env`, then fill in `AMG_BEARER_TOKEN` (generate via `python -c "import secrets; print(secrets.token_urlsafe(32))"`). |
| `MISSING AMG_BEARER_TOKEN` | Edit `~/.config/messaging-agent/.env` and set the token. The adapter refuses to start without it. |
| `MISSING allowlist.toml` | `touch ~/.config/messaging-agent/allowlist.toml` (empty allowlist is valid — every sender will land in quarantine). For real use, populate per spec §5.7. |
| Mode is not `600` | `chmod 600 ~/.config/messaging-agent/.env` |

Then restart:

```bash
amg service restart adapter
```

### Confirm

```bash
amg status adapter
curl -sS http://127.0.0.1:8080/healthz | jq .
# Expected: state=running and {"status": "ok", ...}
```

---

## 2. Discord disconnects repeatedly

**Symptom**: `GET /healthz` reports the Discord connector in a non-`ok` state,
or the structured logs show repeated reconnect / IDENTIFY / RESUME cycles
without sustained delivery. New Discord messages do not appear in
`/messages/unread`.

### Diagnose

```bash
# Filter for Discord connector events (last 200 lines).
amg logs adapter --no-follow -n 500 | \
  grep -E 'discord_(connector|gateway)' | tail -n 200

# Specifically look for close codes and identify failures.
amg logs adapter --no-follow -n 2000 | \
  grep -E 'close_code|invalid_session|identify_failed|disallowed_intents' | tail -n 50

# Healthz state.
curl -sS http://127.0.0.1:8080/healthz | jq '.connectors.discord'
```

Then verify external prerequisites:

```bash
# Token present (and not redacted to empty).
grep -E '^AMG_DISCORD_BOT_TOKEN=.+' ~/.config/messaging-agent/.env >/dev/null \
  && echo "ok: bot token set" \
  || echo "MISSING bot token"

# Discord platform health.
curl -sS https://discordstatus.com/api/v2/status.json | jq '.status.indicator,.status.description'
# Expected indicator: "none"
```

Common close codes and what they mean (see [Discord Gateway docs](https://discord.com/developers/docs/topics/opcodes-and-status-codes#gateway-gateway-close-event-codes)):

- `4004` Authentication failed → bad / revoked / rotated token.
- `4013` Invalid intent(s) → bot is requesting an intent that is not enabled in the developer portal.
- `4014` Disallowed intent(s) → the bot is in 100+ guilds and the intent requires verification, *or* `MESSAGE_CONTENT` was never enabled.
- `4000–4003`, `4005–4009` Generic / recoverable → reconnect is the right behavior; only worry if it loops.

### Recover

1. **Bad token** (`4004`): generate a new token in the
   [Discord Developer Portal](https://discord.com/developers/applications) →
   Bot → Reset Token. Update `AMG_DISCORD_BOT_TOKEN` in
   `~/.config/messaging-agent/.env`. Restart: `amg service restart adapter`.
2. **Disallowed intent** (`4013`/`4014`): Developer Portal → your app → Bot →
   **Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT**.
   No code change required; the connector will pick it up on the next
   reconnect (or restart the adapter to force one).
3. **Discord platform incident**: nothing to do but wait. Inbound messages
   queue on Discord's side and arrive on reconnect. Outbound sends will
   fail-fast and the adapter will retry per its rate-limited send loop.
4. **Local network**: confirm the host can reach `gateway.discord.gg` and
   `discord.com`:
   ```bash
   curl -sSI https://discord.com/api/v10/gateway | head -n 1
   # Expected: HTTP/2 200
   ```

### Confirm

```bash
curl -sS http://127.0.0.1:8080/healthz | jq '.connectors.discord.state'
# Expected: "ok"
```

Send a test message from a real Discord account in an allowlisted DM and
verify it appears in `/messages/unread` within a few seconds.

---

## 3. iMessage stops receiving

**Symptom**: New iMessage messages received in Messages.app on the host Mac
do not appear in `/messages/unread`. `GET /healthz` shows the iMessage
connector idle or stuck. The structured log shows no `imessage_poll`
events for the past minute or more.

### Diagnose

```bash
# Last poll activity (the connector logs each poll cycle and the last ROWID).
amg logs adapter --no-follow -n 2000 | \
  grep -E 'imessage_(connector|poll|cursor)' | tail -n 50

# What ROWID does the connector think it is at?
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT source, cursor, updated_at FROM connector_state WHERE source='imessage';"

# What ROWID does chat.db actually have?
sqlite3 "file:$HOME/Library/Messages/chat.db?mode=ro" \
  "PRAGMA query_only = ON; SELECT MAX(ROWID), datetime('now') FROM message;"
# If chat.db MAX(ROWID) > connector_state.cursor by a lot AND connector hasn't
# advanced in minutes → connector is stuck or asleep.

# Permission check (FDA): does the *adapter* — not Terminal — see chat.db?
ls -l ~/Library/Messages/chat.db
sqlite3 "file:$HOME/Library/Messages/chat.db?mode=ro" \
  "PRAGMA query_only = ON; SELECT COUNT(*) FROM message;" \
  >/dev/null && echo "ok from THIS shell" || echo "FDA denied to THIS shell"
# Note: FDA is per-binary. Terminal succeeding does not mean the adapter has it.

# Mac sleep history.
pmset -g log | grep -iE 'sleep|wake' | tail -n 20

# Messages.app sign-in (heuristic — if the app is not configured, no chat.db
# updates happen):
test -s ~/Library/Messages/chat.db && echo "chat.db exists & non-empty" \
  || echo "chat.db missing/empty — Messages.app not signed in?"

# Is anything keeping the Mac awake?
pmset -g assertions | grep -E 'PreventUserIdleSystemSleep|PreventSystemSleep'
```

### Recover

| Cause | Recovery |
|---|---|
| FDA denied to adapter | Re-grant FDA to the adapter's actual binary path (which may have changed across `uv` cache rebuilds or Python upgrades). See the [Setup Guide](../getting-started/index.md#64-permission-recovery-checklist). Fully restart the adapter. |
| Mac was asleep | Wake the Mac. The connector resumes from the persisted ROWID, so no inbound messages are lost — they will be processed in order. To prevent recurrence: launch the adapter under `caffeinate -dimsu --` or set Energy / Battery → "Prevent automatic sleeping when display is off". |
| Messages.app not signed in | Open Messages.app and sign in with the operator's Apple ID. `chat.db` only receives writes when Messages.app is the running iMessage client. |
| Connector stuck (last poll log > 1 min ago, FDA OK, Mac awake) | Restart the adapter: `amg service restart adapter`. The persisted ROWID cursor (spec §5.3 `connector_state`) ensures no missed rows. |
| `connector_state.cursor` is corrupt or way ahead of real `chat.db` | Manually reset to the highest known-good ROWID: `sqlite3 "$AMG_DB_PATH" "UPDATE connector_state SET cursor=<rowid> WHERE source='imessage';"`. Restart the adapter. **Warning**: setting it lower than current will re-emit messages on the webhook; setting it higher will skip messages. |

### Confirm

```bash
# Connector progresses on its own (poll interval is ~1s).
sleep 5
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT cursor, updated_at FROM connector_state WHERE source='imessage';"
# Expected: updated_at within the last few seconds.
```

Send yourself an iMessage from another device, then:

```bash
curl -sS -H "Authorization: Bearer $AMG_BEARER_TOKEN" \
  -H "X-Agent-ID: runbook-check" \
  http://127.0.0.1:8080/messages/unread | jq '.messages[-1]'
```

---

## 4. AppleScript send always fails

**Symptom**: `POST /messages/send` with an `imsg:...` channel returns 200
(envelope echoed) but the message never appears in Messages.app on the host
Mac. Logs show repeated `applescript_send_failed` or
`osascript_returned_nonzero` entries.

This is almost always the **Automation** TCC grant: macOS gates
cross-application scripting behind a per-(controller, target) prompt that
the adapter's parent process must accept. Until accepted, AppleScript
returns success but Messages.app silently drops the request.

### Diagnose

```bash
# Recent send attempts.
amg logs adapter --no-follow -n 2000 | \
  grep -E 'applescript|osascript' | tail -n 30

# Re-trigger the prompt manually using the same Apple-event a real send uses.
# Run this from the same shell / parent process tree as the adapter
# (otherwise you'll grant Automation to the wrong controller binary).
osascript -e 'tell application "Messages" to get name'
# Expected: prints "Messages"
# If macOS shows a prompt: click OK.
# If it returns "execution error: Not authorized to send Apple events": the
# previous prompt was denied or never shown. Go to Recover.
```

### Recover

1. Trigger the prompt with the no-op script above. Accept it when macOS asks.
2. If you missed the prompt or clicked "Don't Allow", re-enable manually:
   System Settings → **Privacy & Security → Automation** → expand the
   controller (Terminal, iTerm, `uv`, the Python binary, or
   `/usr/bin/caffeinate` if launchd-supervised) → tick the **Messages** box.
3. The Automation grant is **per (controller, target) pair**. Granting
   Automation to Terminal does not grant it to launchd-spawned Python.
   Re-trigger the prompt from the actual adapter parent process.
4. Restart the adapter so the TCC state is re-read fresh:
   `amg service restart adapter`.

If the host has been wedged by repeated denials, a last-resort reset:

```bash
# Clears EVERY app's Automation grants — every controller will be re-prompted
# the next time it tries to send Apple events. Use sparingly.
tccutil reset AppleEvents
```

### Confirm

```bash
curl -sS -X POST http://127.0.0.1:8080/messages/send \
  -H "Authorization: Bearer $AMG_BEARER_TOKEN" \
  -H "X-Agent-ID: runbook-check" \
  -H "Content-Type: application/json" \
  -d '{"channel_id":"imsg:+15551234567","text":"runbook send-check"}'
# Expected: HTTP 200 with envelope echo, AND the message visibly appears
# in Messages.app on the host Mac.
```

If 200 + invisible: Automation was granted to the wrong controller binary.
Repeat step 3.

---

## 5. Webhook dead-lettering all messages

**Symptom**: Spec §5.5 says webhooks retry 5 times then dead-letter. If
*every* delivery is dead-lettering, the receiver is uniformly broken or
mis-configured. The underlying messages stay in `/messages/unread`
(poll-based recovery), but the operator wants webhooks restored.

### Diagnose

```bash
# Per-status counts. Healthy: mostly 'delivered', some 'pending'. Sick: lots of 'dead'.
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT status, COUNT(*) FROM webhook_deliveries GROUP BY status;"

# Most recent dead-letter rows with their last error.
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT id, message_id, attempt, last_response_code, substr(last_error, 1, 80), updated_at
   FROM webhook_deliveries WHERE status='dead' ORDER BY updated_at DESC LIMIT 10;"

# Recent webhook delivery log lines.
amg logs adapter --no-follow -n 2000 | \
  grep -E 'webhook_(deliver|attempt|dead)' | tail -n 50
```

Walk these external checks:

```bash
# 1. Is the receiver reachable from the adapter host?
WEBHOOK_URL=$(grep '^AMG_WEBHOOK_URL=' ~/.config/messaging-agent/.env | cut -d= -f2-)
curl -sS -o /dev/null -w "status=%{http_code} time=%{time_total}s\n" \
  -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d '{"ping":1}'
# DNS error / connection refused / 5xx → receiver problem, not adapter problem.

# 2. Does the adapter's HMAC secret match the receiver's expected secret?
#    The receiver verifies X-AMG-Signature: sha256=<hex> over the *exact* body
#    bytes (spec §5.5). If receiver returns 4xx with a "bad signature" message,
#    the secrets are out of sync.
grep '^AMG_WEBHOOK_SECRET=' ~/.config/messaging-agent/.env

# 3. Is the receiver returning 4xx? (4xx = no retry, immediate dead-letter.)
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT last_response_code, COUNT(*) FROM webhook_deliveries
   WHERE status='dead' GROUP BY last_response_code ORDER BY 2 DESC;"
# 400/401/403 → bad signature / auth. 404 → wrong path. 422 → schema mismatch.
```

### Recover

| `last_response_code` pattern | Likely cause | Fix |
|---|---|---|
| Connection refused / DNS error | Receiver process down or URL wrong | Restart receiver, or correct `AMG_WEBHOOK_URL` in `.env`. |
| 401 / 403 with "bad signature" | HMAC secret mismatch | Re-sync `AMG_WEBHOOK_SECRET` in `.env` with the receiver's expected secret. Both sides must use the same shared secret. |
| 400 / 422 | Receiver schema validation failed | The envelope format is fixed (spec §3). Update the receiver to accept the documented envelope, or fix the receiver-side parser. |
| 404 | Wrong path | Correct `AMG_WEBHOOK_URL`. |
| 5xx | Receiver bug | Fix the receiver. The adapter retries 5xx automatically; consistent dead-letter means all 5 attempts hit 5xx. |
| Mixed | Receiver is flapping | Stabilize the receiver (likely a memory / restart loop on its end). |

After fixing config, reload the adapter to pick up new env values:

```bash
amg service restart adapter
```

The dead-lettered rows are *not* retried automatically — the underlying
messages remain in `/messages/unread` for poll-based recovery (spec §5.5).
If the operator wants to force a re-fan-out for the messages that were
dead-lettered, the supported recovery is consumer-side: have the receiver
call `GET /messages/unread` to drain the queue.

### Confirm

```bash
# Watch the count of 'pending' rise (new messages) and 'delivered' rise as they ship.
watch -n 5 'sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT status, COUNT(*) FROM webhook_deliveries GROUP BY status;"'
```

For a fresh inbound, the row should transition `pending → delivered` within
a few seconds. New `dead` rows after the fix indicate the receiver is still
broken.

---

## 6. Disk filling from attachments

**Symptom**: Disk usage of the attachment store is much higher than expected,
or `df -h` on the volume is approaching full. The adapter still works but
will eventually fail to re-host new attachments (`WARN` log; envelope ships
text-only per spec §4.5).

### Diagnose

```bash
# Total attachment-store size.
du -sh ~/Library/Application\ Support/messaging-agent/attachments

# Largest 20 attachment files.
find ~/Library/Application\ Support/messaging-agent/attachments \
  -type f -exec du -h {} + 2>/dev/null | sort -rh | head -n 20

# How many attachment rows do we have, and how old are they?
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" <<'SQL'
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN bytes_path IS NOT NULL THEN 1 ELSE 0 END) AS with_bytes,
  SUM(CASE WHEN bytes_path IS NULL THEN 1 ELSE 0 END) AS swept,
  MIN(created_at) AS oldest,
  MAX(created_at) AS newest,
  SUM(size_bytes) AS total_bytes
FROM attachments;
SQL

# Current sweeper threshold.
grep '^AMG_ATTACHMENT_RETENTION_DAYS=' ~/.config/messaging-agent/.env

# Did the sweeper actually run lately? (logs an entry per sweep cycle)
amg logs adapter --no-follow -n 2000 | \
  grep -E 'attachment_(sweep|retention)' | tail -n 20

# How many rows are older than the current threshold and still on disk?
THRESHOLD=$(grep '^AMG_ATTACHMENT_RETENTION_DAYS=' \
  ~/.config/messaging-agent/.env | cut -d= -f2)
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT COUNT(*) FROM attachments
   WHERE bytes_path IS NOT NULL
     AND created_at < datetime('now', '-${THRESHOLD:-90} days');"
# A non-zero result here while the sweeper exists means the sweeper isn't running
# (or its clock is off).
```

### Recover

1. **Lower the retention window** so the next sweep deletes more:
   ```bash
   # Edit ~/.config/messaging-agent/.env and reduce e.g. 90 → 14.
   sed -i '' 's/^AMG_ATTACHMENT_RETENTION_DAYS=.*/AMG_ATTACHMENT_RETENTION_DAYS=14/' \
     ~/.config/messaging-agent/.env
   ```
2. **Force the sweep** (the sweeper runs on a daily cadence; restart re-arms
   it for an immediate cycle on startup):
   ```bash
   amg service restart adapter
   ```
3. If the sweeper itself isn't running (no `attachment_sweep` log entries
   since the last restart), check `connector_state` and the sweep task in
   recent logs — it should fire shortly after startup. File a bug if it
   doesn't; in the meantime, fall back to manual cleanup:
   ```bash
   # CAUTION: deletes attachment bytes older than 14 days from BOTH disk and the
   # bytes_path column. The attachment row stays so messages still reference it.
   sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" <<'SQL'
   SELECT bytes_path FROM attachments
   WHERE bytes_path IS NOT NULL
     AND created_at < datetime('now', '-14 days');
   SQL
   # Review the list, then for each path: rm "$path" and
   # UPDATE attachments SET bytes_path=NULL WHERE id='...'
   ```

### Confirm

```bash
du -sh ~/Library/Application\ Support/messaging-agent/attachments
# Should be markedly smaller. Re-check df -h.

sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT COUNT(*) FROM attachments WHERE bytes_path IS NOT NULL;"
# Should drop after the sweep runs.
```

---

## 7. Migration failed

**Symptom**: After `git pull && uv sync && alembic upgrade head` (the
documented update path; spec §11.1), `alembic upgrade` exits non-zero, the
adapter refuses to start, or a partially-applied migration leaves the DB
in a half-state. Logs show `alembic.runtime.migration` errors.

### Diagnose

```bash
# Where is alembic right now?
uv run alembic current
# Expected: a single revision ID + "(head)" if up to date.

# Show the migration history; where did it stop?
uv run alembic history --verbose | head -n 40

# What did the failure actually say? Re-run with verbose output.
uv run alembic upgrade head 2>&1 | tail -n 80

# Take a defensive snapshot before touching anything.
cp "$HOME/Library/Application Support/messaging-agent/amg.db" \
   "$HOME/Library/Application Support/messaging-agent/amg.db.before-recovery-$(date +%s)"
```

### Recover

The recovery sequence is **always**: roll back, fix the migration, re-apply.

```bash
# 1. Roll back exactly one revision. (`-1` is the current head minus one.)
uv run alembic downgrade -1

# 2. Confirm we are now at the previous good revision.
uv run alembic current

# 3. Fix the failing migration. Edit the offending file under
#    amg/db/migrations/versions/ (the path printed by `alembic history`).
#    Common fixes:
#      - SQLite-specific syntax issues (use `op.batch_alter_table` for column
#        rename / drop on SQLite, since SQLite has limited ALTER support).
#      - Forgotten downgrade() body — Alembic needs both directions to be valid.
#      - DDL that conflicts with existing data; add a data-migration step.

# 4. Dry-run the SQL Alembic would execute, without applying it.
uv run alembic upgrade head --sql > /tmp/alembic-dryrun.sql
less /tmp/alembic-dryrun.sql

# 5. Apply for real.
uv run alembic upgrade head

# 6. Restart the adapter.
amg service restart adapter
```

If the downgrade itself fails (broken `downgrade()` in the new revision):
restore from the snapshot and re-apply forward to the prior known-good
revision:

```bash
# DESTRUCTIVE — overwrites the live DB with the snapshot.
cp "$HOME/Library/Application Support/messaging-agent/amg.db.before-recovery-<ts>" \
   "$HOME/Library/Application Support/messaging-agent/amg.db"
uv run alembic stamp <prior-good-revision-id>
# Then fix and re-upgrade.
```

### Confirm

```bash
uv run alembic current  # should show the new head
curl -sS http://127.0.0.1:8080/healthz | jq .
# Adapter starts without DB-schema errors.
```

---

## 8. chat.db schema changed (future macOS release)

**Symptom**: After a macOS upgrade, the iMessage connector starts logging
errors like `no such column: <name>`, `KeyError` during typedstream decode,
or `sqlite3.OperationalError`. Inbound iMessage messages stop appearing in
`/messages/unread`, but Discord continues working normally. This is **R-3**
in spec §13.

The mitigation is built in: every inbound message persists the original
chat.db row in `messages.raw_json` (spec §5.3, R-3, R-11). That preserves
the source of truth for the operator to adapt the decoder against.

### Diagnose

```bash
# Confirm it's the iMessage connector specifically.
amg logs adapter --no-follow -n 5000 | \
  grep -E 'imessage_(decode|chatdb|schema)' | tail -n 50

# What macOS version is this Mac on?
sw_vers -productVersion

# What does the live schema look like? Compare against the pinned version
# the codebase tests against (tests/fixtures/build_chat_db.py — see Phase 0
# findings).
sqlite3 "file:$HOME/Library/Messages/chat.db?mode=ro" \
  "PRAGMA query_only = ON; .schema message" | head -n 60

# Pull a few raw_json snapshots from the affected window to compare against
# what the decoder expected.
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT id, source, message_ts, substr(raw_json, 1, 400)
   FROM messages WHERE source='imessage'
   ORDER BY message_ts DESC LIMIT 5;"
```

### Recover (this is a code-change recovery, not a config-change recovery)

1. Capture the live schema and a representative `raw_json` payload (above).
2. Diff the live schema against `tests/fixtures/build_chat_db.py` — that
   file is the schema the codebase pins and tests against.
3. Update the connector's chat.db reader to handle the schema delta:
   - **New / renamed column**: update the SELECT and the row-to-envelope
     mapping in the iMessage connector (`amg/connectors/imessage/`).
   - **`attributedBody` typedstream layout change**: extend the typedstream
     decoder. The known-good layout is documented in
     `internal/notes/phase0-findings.md` ("attributedBody typedstream archive").
   - **mach-time / `date` change**: unlikely (Apple has held this for
     decades), but recompute via `date / 1_000_000_000 + APPLE_EPOCH_OFFSET`.
4. Add a fixture row in `tests/fixtures/build_chat_db.py` that exercises the
   new shape; assert the decoder produces the expected envelope.
5. Roll out: `git pull && uv sync --all-packages && uv run alembic upgrade
   head && amg service restart adapter`.

The `messages.raw_json` rows captured during the broken window let you
re-decode and re-emit historical messages once the fix is in (a one-shot
backfill script over `WHERE source='imessage' AND created_at > <ts>`),
since none of the source data is lost.

### Confirm

```bash
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT cursor, updated_at FROM connector_state WHERE source='imessage';"
# updated_at advances; cursor crosses the post-upgrade ROWID range without errors.

amg logs adapter --no-follow -n 5000 | \
  grep -c 'imessage_decode_error'
# Trends to zero.
```

---

## 9. Identity-link split-brain

**Symptom**: A single human appears as two distinct senders to the agent
(e.g., the agent treats their iMessage and Discord identities as two
different people, breaking cross-platform context). This is the failure
mode tracked as **R-10** in spec §13.

The allowlist's `person_id` field (spec §5.7) is what links cross-platform
identities. A mistyped `person_id` (e.g. `alice` on iMessage, `alise` on
Discord) is the canonical cause: each is unique to *one* allowlist entry,
so the loader can't materialize a meaningful `identity_links` row, and the
adapter logs a `WARN`.

### Diagnose

```bash
# 1. Look for the loader warning. The allowlist loader logs a WARN when a
#    person_id is referenced from only a single entry — exactly the
#    split-brain signal.
amg logs adapter --no-follow -n 5000 | \
  grep -E 'person_id.*(single|alone|one_entry|split)' | tail -n 50

# 2. Inspect the materialized identity_links table — how many rows per person_id?
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT person_id, COUNT(*) AS link_count
   FROM identity_links GROUP BY person_id ORDER BY link_count;"
# Healthy cross-platform link: link_count >= 2 (one per source).
# Split-brain: link_count = 1 for a person_id that *should* span platforms.

# 3. Cross-check senders by display_name to spot duplicates that should be one.
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT display_name, source, sender_id, person_id
   FROM senders ORDER BY display_name;"
# Two rows with the same display_name but different person_id values
# → split-brain in the allowlist.

# 4. Read the allowlist file directly to see the typo.
cat ~/.config/messaging-agent/allowlist.toml
```

### Recover

1. Edit `~/.config/messaging-agent/allowlist.toml`. Make the typo'd entries
   share the same `person_id`. Example:
   ```toml
   # WRONG — "alice" only on iMessage, "alise" only on Discord = split-brain.
   [[entry]]
   source = "imessage"
   id = "+15551234567"
   person_id = "alice"

   [[entry]]
   source = "discord"
   id = "123456789012345678"
   person_id = "alise"   # typo

   # RIGHT — both entries share person_id "alice".
   [[entry]]
   source = "imessage"
   id = "+15551234567"
   person_id = "alice"

   [[entry]]
   source = "discord"
   id = "123456789012345678"
   person_id = "alice"
   ```
2. Reload the allowlist (no restart needed; spec §5.7 supports SIGHUP):
   ```bash
   # Get the supervised adapter PID via amg status, then SIGHUP it.
   PID=$(amg status adapter --json | jq -r '.pid')
   kill -HUP "$PID"
   ```

   `identity_links` is rebuilt on SIGHUP from the new allowlist (spec §5.3,
   "Notes" on `identity_links`). Note the spec §5.7 caveat: in-flight
   message rows that already resolved against the old allowlist are **not**
   recomputed — only newly-arriving messages and the materialized
   `identity_links` table reflect the fix. (Cross-reference OQ-4.)

### Confirm

```bash
# After SIGHUP — every cross-platform person_id now has link_count >= 2.
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT person_id, COUNT(*) AS link_count
   FROM identity_links GROUP BY person_id ORDER BY link_count;"

# The WARN should not re-appear on the next SIGHUP-reload log line.
amg logs adapter --no-follow -n 500 | \
  grep -E 'allowlist_(load|reload)' | tail -n 5
```

---

## Quick reference: log and DB queries

The same handful of commands cover ~80% of diagnosis. Bookmark these.

```bash
# Live follow of structured adapter logs, errors only.
amg logs adapter | grep level=error

# Today's adapter log (last 500 lines, no follow).
amg logs adapter --no-follow -n 500

# Launchd spawn output (process-level crashes show here, not in the structured log).
amg logs adapter --launchd --no-follow -n 200

# Bundled diagnostics — FDA, .env, allowlist, service state, healthz.
amg doctor

# Service state.
amg status

# Health.
curl -sS http://127.0.0.1:8080/healthz | jq .

# Webhook delivery state.
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT status, COUNT(*) FROM webhook_deliveries GROUP BY status;"

# Connector cursors (where each connector left off).
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT source, cursor, updated_at FROM connector_state;"

# Quarantine size (allowlist_status='unknown' messages — if growing fast,
# operator probably needs to add a sender to the allowlist).
sqlite3 "$HOME/Library/Application Support/messaging-agent/amg.db" \
  "SELECT COUNT(*) FROM messages WHERE allowlist_status='unknown';"

# Disk usage of the attachment store.
du -sh ~/Library/Application\ Support/messaging-agent/attachments

# Restart the adapter (in-place; preserves launchd supervision).
amg service restart adapter

# Reload allowlist without a restart (SIGHUP the supervised PID).
kill -HUP "$(amg status adapter --json | jq -r '.pid')"
```

## Escape hatch: raw `launchctl`

Routine ops should never need raw `launchctl`. If `amg` itself is broken
or the host has wedged into an unrecoverable launchd state, drop down to
the underlying domain commands as a last resort:

```bash
launchctl print "gui/$(id -u)/com.user.amg-adapter"
launchctl bootout "gui/$(id -u)/com.user.amg-adapter"
```

Use sparingly; file an issue describing what `amg` couldn't recover from.

## See also

- [Setup Guide](../getting-started/index.md) — full macOS permission flow (FDA,
  Automation, sleep prevention) referenced from scenarios 1, 3, and 4.
- [`ops/launchd/README.md`](https://github.com/sequenzia/amg/blob/main/ops/launchd/README.md)
  — launchd plist install, uninstall, update commands; logs paths.
- [`specs/agent-messaging-gateway-SPEC.md`](https://github.com/sequenzia/amg/blob/main/specs/agent-messaging-gateway-SPEC.md)
  — the source of truth: §5.3 (storage schema), §5.5 (webhook retry policy),
  §5.7 (allowlist), §11.2 (env vars), §11.4 (this runbook's authoritative
  scenario list), §13 (risks R-1, R-3, R-5, R-8, R-10, R-11).
