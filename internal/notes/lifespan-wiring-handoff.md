# Lifespan wiring — handoff & next steps

**Date:** 2026-05-09
**Context:** `amc/app.py` lifespan now boots the bearer token, DB, allowlist, message sink, webhook worker, sweepers, and (gated) connectors. This document covers what's left for the operator and which test failures predate this work but should be tracked.

---

## What was wired

Lifespan in `amc/app.py` now performs (in order):

0. **Hydrate `os.environ` from `~/.config/messaging-agent/.env`** (process env wins on conflict). Without this step, only `load_bearer_token()` could see `.env` values; everything else only reads `os.environ`.
1. `load_bearer_token()` — reads `AMC_BEARER_TOKEN` from `~/.config/messaging-agent/.env` or process env.
2. `load_allowlist()` — reads `AMC_ALLOWLIST_PATH` (default `~/.config/messaging-agent/allowlist.toml`).
3. `load_webhook_config_from_env()` — webhook config (None when `AMC_WEBHOOK_URL` is unset).
4. `create_engine_from_env()` + `create_session_factory()` — SQLite engine on `app.state.engine` / `app.state.session_factory`.
5. `WebhookWorker` constructed first so its `url_snapshot` can be shared with…
6. `MessageSink` — single chokepoint, on `app.state.sink`.
7. `AttachmentStore` (with `bind_url` from `AMC_BIND_HOST`/`AMC_BIND_PORT`), `RateLimiter.from_env()`, `IdempotencyStore`, `IdempotencySweeper`, `AttachmentSweeper`.
8. **Discord connector** — only when `AMC_DISCORD_BOT_TOKEN` is set; registered with `healthz.register_connector("discord", ...)`.
9. **iMessage connector** — only when `sys.platform == "darwin"` and `~/Library/Messages/chat.db` exists; registered the same way.
10. Wires every route-level singleton via `configure_session_factory` / `configure_rate_limiter` / `configure_idempotency_store` / `configure_discord_connector` / `configure_imessage_connector`.
11. Starts: webhook worker, idempotency sweeper, attachment sweeper.
12. Reverse-order shutdown: sweepers → worker → iMessage → Discord → `engine.dispose()`.

`build_app(bootstrap: bool = True)` — production passes the default; tests that pre-configure singletons manually pass `bootstrap=False` so the lifespan is inert.

---

## What you need to do next

### To enable Discord

1. Put the bot token in `~/.config/messaging-agent/.env` (mode 0600):
   ```
   AMC_DISCORD_BOT_TOKEN=...
   ```
2. Restart `uvicorn amc.app:app`.
3. Verify:
   ```bash
   curl -s -H "Authorization: Bearer $AMC_BEARER_TOKEN" \
     http://127.0.0.1:8080/healthz | python3 -m json.tool
   ```
   `connectors.discord.state` should be `"ok"`. If it's `"degraded"`, the bot token was rejected — see RUNBOOK §"Discord auth".

### To enable iMessage receive/send (macOS only)

1. **Full Disk Access** — System Settings → Privacy & Security → Full Disk Access → add the binary that runs uvicorn (Terminal, iTerm, or whatever you launch from). Without this, `chat.db` is unreadable and the connector goes degraded immediately.
2. **Automation prompt** — the first outbound send triggers a one-time prompt to allow the process to control Messages. Until accepted, sends silently fail. Trigger it deliberately by sending a test message early.
3. **Keep the Mac awake** — `caffeinate -dimsu` in a separate process, or set Energy preferences to never sleep. The poller stops if the Mac sleeps.
4. Verify by sending yourself an iMessage from another device and hitting `/messages/unread`.

### To enable webhook delivery

```
AMC_WEBHOOK_URL=https://your-receiver.example/hooks/amc
AMC_WEBHOOK_SECRET=<generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`>
```

Then restart. Worker logs `webhook_worker_started` on boot. Inspect `webhook_deliveries` table for queue state, or read `webhook_queue.pending`/`webhook_queue.dead` from `/healthz`.

### Operational prerequisites (one-time)

- Run migrations: `uv run alembic upgrade head` — but **make sure `AMC_DB_PATH` is consistent**. The spec/`.env.example` default is `amc.db`; the code-level default in `amc/core/db.py:54-56` is `state.db`. Migrate the file your `.env` points at, not the code default. (Tracked divergence: `internal/notes/spec-code-divergences.md`.)
- Seed `~/.config/messaging-agent/allowlist.toml` with at least one `[[entry]]` block per allowed sender (see `internal/adrs/0005-allowlist-toml.md`).

### Recommended verification flow

1. `curl /healthz` → `status: "ok"`, both connectors `"ok"` (or `"disabled"` for whichever you didn't enable).
2. Send a Discord DM to the bot from an allowlisted user → row appears in `/messages/unread`.
3. `POST /messages/send` to that channel → reply lands in Discord.
4. Same flow for iMessage if enabled.
5. If using webhooks, your receiver should fire on inbound — check the `webhook_deliveries` table for `status='delivered'`.

---

## Test failures that should be fixed

These were already failing before the lifespan work and are unrelated to it. Tracking them here so they don't get lost.

### 1. `tests/core/test_webhook.py` — 14 failing tests

**Symptom:** `respx.mock()` reports "some routes were not called" because `WebhookWorker.tick()` never fires the HTTP request that the test set up a respx route for.

```
AssertionError: RESPX: some routes were not called!
assert [<Route <Scheme eq 'https'> AND <Host eq 'receiver.example'> ...>] == []
```

**Likely cause:** respx version mismatch with the httpx version `WebhookWorker` uses internally — respx's transport interception isn't catching the calls. This is the same family of issue called out in `CLAUDE.md` for the Discord connector (`respx` can't intercept aiohttp), but here it's httpx so it should be interceptable. Worth comparing the resolved `respx` and `httpx` versions in `uv.lock` against what worked when these tests last passed.

**Affected tests** (all in `tests/core/test_webhook.py`):
- `test_tick_5xx_schedules_retry_with_first_backoff`
- `test_full_backoff_schedule_dead_letters_after_5_attempts`
- `test_tick_skips_rows_whose_next_retry_at_is_in_the_future`
- `test_tick_network_error_treated_as_5xx_and_retried`
- `test_tick_timeout_treated_as_5xx_and_retried`
- `test_in_flight_retry_uses_url_captured_at_queue_time`
- `test_worker_picks_up_pending_rows_after_restart`
- `test_tick_processes_rows_in_next_retry_at_ascending_order`
- `test_tick_4xx_dead_letters_immediately[422]`
- (and ~5 more in the same module)

**Fix path:** check `respx==0.23.1` vs current `httpx`. If versions are mismatched, pin the working pair in `pyproject.toml`. If the API surface changed, update the test fixture's `respx.mock()` calls to the new style.

### 2. `tests/e2e/test_webhook_retry.py` — 2 failing tests

Likely the same `respx`/`httpx` issue cascading; e2e webhook flow can't observe the requests. Fixing #1 should fix these.

- `test_full_backoff_dead_letters_then_message_still_unread`
- `test_4xx_response_dead_letters_immediately`

### 3. `tests/fixtures/test_chat_db_fixture.py::test_attachment_row_links_to_real_file`

A single fixture-level test about chat.db attachments. Investigate independently — could be a file-path expectation that drifted, or a mach-time conversion edge case.

### 4. `tests/core/test_allowlist.py::test_load_allowlist_falls_back_to_default`

**Symptom:** test asserts `load_allowlist()` raises `AllowlistError` when the env var is unset, *because* the default path `~/.config/messaging-agent/allowlist.toml` is expected to be missing on the test host. On any developer machine that has AMC configured, the file exists and `load_allowlist()` succeeds, so the `pytest.raises` block sees no exception → fails.

**Fix path:** change the test to monkeypatch `DEFAULT_ALLOWLIST_PATH` to a guaranteed-missing tmp_path entry, or use `pytest.MonkeyPatch.setattr` on the module constant. The current test only works on a clean host.

### 5. (Possibly cosmetic) `tests/api/test_messages_unread.py` — `PytestUnknownMarkWarning`

```
PytestUnknownMarkWarning: Unknown pytest.mark.slow
```

Either register `slow` in `pyproject.toml` `[tool.pytest.ini_options]`'s `markers = [...]`, or remove the marker.

---

## Files changed in this session

```
M  amc/app.py                                     — full lifespan wiring + bootstrap=True kwarg
M  tests/api/golden/openapi.json                  — regenerated to include /healthz
M  tests/e2e/test_discord_roundtrip.py            — build_app(bootstrap=False)
M  tests/e2e/test_webhook_retry.py                — build_app(bootstrap=False)
M  tests/e2e/test_crash_relaunch.py               — build_app(bootstrap=False)
M  tests/e2e/test_imessage_roundtrip.py           — build_app(bootstrap=False)
M  tests/stability/harness.py                     — build_app(bootstrap=False)
```

The pre-existing changes in `.gitignore`, `tests/fixtures/chat.db`, and `mcp-wrapper/bun.lock` are not part of this work and were already in the working tree.
