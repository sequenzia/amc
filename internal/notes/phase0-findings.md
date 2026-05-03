# Phase 0 — Test Fixture Findings

This note captures everything we learned while building the Phase 0
fixtures and fakes (spec §9.0). It exists so a future implementer
working on the iMessage connector (§9.2), the Discord connector
(§9.1), or a swap-out of either fake doesn't have to re-derive these
from first principles.

## 1. macOS `chat.db` quirks

The fixture is hand-built by `tests/fixtures/build_chat_db.py`. Everything
below was discovered while making that script produce a byte-identical
SQLite file across runs and remain queryable with the same SQL the
production iMessage connector will use.

### 1.1 Schema slimming

Real `~/Library/Messages/chat.db` ships with several `CREATE TRIGGER`
statements (e.g. `verify_chat`, `delete_chat_background_before_deleting_chat`,
`delete_attachment_path`) that call C functions only resolvable inside
Messages.app. Including any of them in the fixture causes
`sqlite3.OperationalError: no such function: verify_chat` the moment the
DB is opened. **Strip every trigger** from any schema dump before
shipping it as a test fixture. We only retain tables, indexes, and the
columns the connector actually reads.

The minimum table set is **seven**:

* `handle` — sender identity (E.164 phone numbers / Apple IDs).
* `chat` — one row per thread, keyed by `guid`.
* `chat_handle_join` — many-to-many: which handles participate in which chats.
* `message` — one row per message, with the `text` column or
  `attributedBody` blob.
* `chat_message_join` — many-to-many: which messages belong to which chat.
* `attachment` — one row per attachment, includes the on-disk path.
* `message_attachment_join` — many-to-many between messages and attachments.

### 1.2 Handle table joins

Resolving "who sent this message and in what thread" requires a four-way
join. The exact join the connector planning notes call out is:

```sql
SELECT m.guid, m.text, h.id AS handle_id, c.guid AS chat_guid
FROM message m
JOIN handle h            ON h.ROWID = m.handle_id
JOIN chat_message_join j ON j.message_id = m.ROWID
JOIN chat c              ON c.ROWID = j.chat_id
WHERE c.guid = ? AND h.id = ?
```

Three traps lurk in here:

1. **`handle_id` is a ROWID, not the E.164 string.** Every join into
   `handle` must be on `ROWID`, never on the freeform `id` column. The
   `id` column is what gets surfaced to the agent (`+15551234567`); it
   is not a foreign key.
2. **`message.handle_id` can be `NULL` for outbound messages** (the
   user is the sender). The fixture sticks to inbound for v1 to avoid
   modeling the `is_from_me` semantics — when v1 grows outbound
   read-back support, the join above must be a `LEFT JOIN` with a
   conditional fallback.
3. **`chat.guid`** is a structured string, not a UUID. iMessage DMs use
   the form `iMessage;-;<handle>` (semicolon-separated; the `-` is the
   "not a group" slot). The fixture's `CHAT_GUID` constant
   (`"iMessage;-;+15551234567"`) demonstrates the shape and is the value
   the AppleScript sender will eventually quote in
   `tell application "Messages" to send "..." to chat id "<chat.guid>"`.

### 1.3 `attributedBody` decode path

Real Messages.app sometimes leaves `message.text` as `NULL` and stuffs
the body into `message.attributedBody` as a binary blob. **This is not
NSKeyedArchiver / bplist.** It is the older Apple **typedstream** archive
format (the same format `NSArchiver` predates `NSKeyedArchiver` writes).
The on-disk layout, in order, is:

| Bytes              | Meaning                                                 |
|--------------------|---------------------------------------------------------|
| `\x04 \x0B`        | Magic prefix (typedstream marker)                       |
| `streamtyped`      | ASCII literal — full magic is `\x04\x0Bstreamtyped`     |
| `\x81 \xe8 \x03`   | Version / endianness markers                            |
| Class hierarchy    | `NSAttributedString -> NSObject` then `NSString` class  |
| Length byte        | UTF-8 length of the message body, **single byte**       |
| UTF-8 payload      | Message text                                            |
| Attribute-run dict | Trailing dict including `__kIMMessagePartAttributeName=0` |

The fixture builder hand-emits this byte sequence in
`tests/fixtures/build_chat_db.make_attributed_body()`. Two limitations
fell out of getting it right:

* **Single-byte length prefix** means message bodies > 127 UTF-8 bytes
  cannot use this code path as-written. The builder raises
  `ValueError("short strings ...")` if you try; extending to multi-byte
  prefixes (the typedstream `0x81` / `0x82` length markers) is mechanical
  but unnecessary for v1's seeded fixtures.
* `NSKeyedArchiver` Python tools (`ccl_bplist`, `bplist3`) **will not
  decode this**. The eventual connector decoder will need either a
  hand-rolled typedstream walker or a vendored copy of
  `imessage_reader`'s `attributedBody` parser. This is the single
  largest piece of unknown work for §9.2.

### 1.4 Attachment join semantics

`message_attachment_join` is straightforward many-to-many, but two
non-obvious facts apply:

* `attachment.filename` stores an **absolute on-disk path** that lives
  outside `chat.db`. On a real Mac that's something like
  `~/Library/Messages/Attachments/<two hex bytes>/<UUID>/<filename>`.
  The fixture stores a path under `tests/fixtures/attachments/` and the
  build script asserts that the file actually exists before finalizing
  the DB — a `chat.db` with a dangling attachment row is a real-world
  bug we don't want shipped in the test data.
* `attachment.total_bytes` must match `os.path.getsize(filename)`. The
  fixture enforces this; the connector will need to as well, because
  Messages occasionally writes a row first and the file later (race
  during incoming MMS), and we want to detect the mismatch rather than
  silently truncate.

### 1.5 Time stamps

`message.date` is **mach absolute time**: nanoseconds since
`2001-01-01T00:00:00Z` (Apple's reference epoch, NOT the unix epoch).
The fixture hard-codes `_BASE_DATE_NS = 767_287_931_000_000_000`
(equivalent to `2026-04-25T15:32:11Z`). When the connector converts to
the spec envelope's ISO-8601 `created_at`, the formula is:

```
unix_seconds = (mach_ns / 1_000_000_000) + 978_307_200
```

`978307200` = unix seconds between `1970-01-01` and `2001-01-01`.

### 1.6 Read-only open

The connector opens `chat.db` with the `mode=ro` URI **plus**
`PRAGMA query_only = ON`. URI mode-ro alone is insufficient because
SQLite still attempts journal recovery on open. The smoke test
(`tests/fixtures/test_phase0_smoke.py::test_chat_db_loads_via_connector_read_path`)
asserts both: (a) the read works, (b) `INSERT` raises
`sqlite3.OperationalError`.

### 1.7 Determinism

Reproducing a byte-identical `chat.db` across two `build()` calls
requires:

* `PRAGMA journal_mode = DELETE` (the default `WAL` produces sidecar
  files and non-deterministic page ordering).
* `PRAGMA page_size = 4096` set at create time (cannot be changed after
  rows exist without `VACUUM`).
* `PRAGMA application_id = 0x6D656473` (the macOS Messages magic; not
  required for correctness but pins a stable header byte).
* Hard-coded `ROWID`, `guid`, and timestamp values everywhere — no
  `secrets`, no clock reads, no environment lookups.
* A final `VACUUM` to canonicalize page ordering.
* Atomic file rename via `tmp_path.replace(final_path)`.
* Clean-up of `*-wal`, `*-shm`, `*-journal` sidecars before the rebuild
  starts.

Deterministic-build proof lives in
`tests/fixtures/test_chat_db_fixture.py::test_build_is_deterministic`.

## 2. Fake gateway protocol coverage map

`tests/fakes/discord_gateway.py` implements the Discord gateway
WebSocket server end of the protocol, just enough to drive `discord.py`.
The map below is the authoritative reference for what's wired up vs.
what's intentionally stubbed.

### 2.1 Opcodes

| Opcode | Name              | Direction           | Status                      |
|--------|-------------------|---------------------|-----------------------------|
| 0      | DISPATCH          | server -> client    | **Implemented** (READY, RESUMED, MESSAGE_CREATE) |
| 1      | HEARTBEAT         | client -> server    | **Implemented** (echoes HEARTBEAT_ACK)           |
| 2      | IDENTIFY          | client -> server    | **Implemented** (begins session, dispatches READY) |
| 3      | PRESENCE_UPDATE   | client -> server    | Silently accepted, no-op    |
| 4      | VOICE_STATE_UPDATE| client -> server    | Silently accepted, no-op    |
| 6      | RESUME            | client -> server    | **Implemented** (replays buffered events)       |
| 7      | RECONNECT         | server -> client    | **Implemented** (via `disconnect()` driver API)  |
| 8      | REQUEST_GUILD_MEMBERS | client -> server | Silently accepted, no-op |
| 9      | INVALID_SESSION   | server -> client    | **Implemented** (sent on unknown session_id)     |
| 10     | HELLO             | server -> client    | **Implemented** (sent on connect)                |
| 11     | HEARTBEAT_ACK     | server -> client    | **Implemented** (sent in response to op 1)       |

### 2.2 Dispatch events (`t` field on op 0)

| Event            | Status                                                     |
|------------------|------------------------------------------------------------|
| `READY`          | **Implemented** — full `user`, `session_id`, `resume_gateway_url`, `application` fields populated so `discord.py`'s `parse_ready` does not crash. |
| `RESUMED`        | **Implemented** — fired after a successful `op 6 RESUME`.  |
| `MESSAGE_CREATE` | **Implemented** — driven by `gateway.deliver(payload)`. Buffered for RESUME replay. |
| Everything else  | Stubbed — no `GUILD_CREATE`, `TYPING_START`, `PRESENCE_UPDATE`, `MESSAGE_UPDATE`, `MESSAGE_DELETE`, voice events, interaction events, etc. The connector v1 only consumes MESSAGE_CREATE so this is fine; later phases that need GUILD_CREATE for permissions resolution must extend the fake. |

### 2.3 Transport-level decisions

* **Frames are always TEXT JSON.** Real Discord can negotiate
  `compress=zlib-stream` or `compress=zstd-stream` and send BINARY
  frames. The fake never advertises compression, so `discord.py`'s
  decompression branch is bypassed entirely. This dropped roughly a day
  of fixture work.
* **The fake hosts on an ephemeral port** (`port=0`) and exposes the
  resulting URL via `gateway.url`. Tests redirect `discord.py` by
  setting `DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(gateway.url)` —
  this is mutable global state, see §3.
* **Close codes 4000–4999** are treated as recoverable by `discord.py`
  (it will reconnect and try to RESUME). `disconnect()` uses 4000.
  Codes 4004 / 4010–4014 are non-recoverable; the fake intentionally
  avoids them.
* **RESUME buffer cap** is 256 events (`RESUME_BUFFER_LIMIT`). Real
  Discord's window is shorter and undocumented; 256 is generous enough
  that no test in v1 has dropped an event mid-replay.

### 2.4 What is *not* covered

* No heartbeat-timeout enforcement. The fake will accept a client that
  never sends a heartbeat.
* No sharding logic beyond reporting `[0, 1]` in READY.
* No voice gateway (port-forward style). Voice is an entirely separate
  WebSocket connection in real Discord.
* No interaction (slash command) dispatch.
* No permission / member cache. Tests that need `Member` objects must
  build them by hand from raw payloads.

## 3. Test ergonomics decisions

### 3.1 Fixture scopes

* `chat_db_path` (in both `test_chat_db_fixture.py` and
  `test_phase0_smoke.py`) is **session-scoped**. The build is
  deterministic and the file is committed; re-materializing it per test
  would burn ~50ms per test for no gain.
* `ro_conn` (in `test_chat_db_fixture.py`) is **function-scoped** —
  each test gets a fresh sqlite3 connection. Sharing connections
  across tests creates implicit ordering coupling that we want to
  avoid.
* `gateway` (in `test_discord_gateway.py`) is **function-scoped**
  because each test wants a fresh server with no buffered events from
  the previous test. The gateway binds an ephemeral port and tears
  down cleanly in well under 100ms.
* `discord_client` (in `test_discord_rest.py`) is **function-scoped**.
  We never start the network loop on it; we only need
  `client._connection` so `make_test_channel` can produce a working
  `PartialMessageable`.
* The smoke test reuses the same scopes its underlying fixtures use.
  We resisted the temptation to share a single Discord client across
  the gateway and REST smoke tests — the bypass-`login` shim mutates a
  class attribute on `DiscordWebSocket` (see below) and we don't want
  cross-test contamination.

### 3.2 Bypassing `discord.py`'s login

The Phase 0 fakes need to drive a real `discord.Client` without ever
contacting Discord. The cleanest interception point we found, after
several false starts, is to subclass `Client` and override
`login(token)` to:

1. Call `_async_setup_hook()` so the internal state machine is
   initialized.
2. Manually construct an `aiohttp.ClientSession` with the right
   `ws_response_class=DiscordClientWebSocketResponse` and stash it on
   `http._HTTPClient__session` (Python name-mangling: `__session` on
   `HTTPClient` becomes `_HTTPClient__session`).
3. Set `http._global_over` to a pre-set `asyncio.Event` so the global
   rate limiter never blocks.
4. Set `http.token` directly.
5. Stash a placeholder `user`/`application_id` on `_connection` so any
   pre-READY access doesn't crash; READY's payload immediately
   replaces them.
6. Set `DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(fake_url)`.

Two things we tried and abandoned:

* **`unittest.mock.patch.object(Client, "login", ...)`** — too coarse;
  also masks the production `login` flow we want to keep exercising
  on real-deployment paths.
* **A bound-method swap (`client.login = bound_replacement`)** — binds
  `self` incorrectly and `discord.py`'s async start code reaches in
  with the unbound method anyway.

### 3.3 Bypassing `discord.py`'s REST

`discord.py` uses `aiohttp` internally (NOT `httpx`), so `respx`
**cannot** intercept its REST traffic. After exploring four options
(monkey-patch `aiohttp.ClientSession.request`, custom `Connector`,
custom `ws_response_class`, and direct `HTTPClient.request` patching),
we landed on `unittest.mock.patch.object(discord.http.HTTPClient,
"request", closure)`. The closure form (not a bound method) is
required because `discord.py` calls the patched method on the class,
which would otherwise produce an incorrectly-bound `self`.

### 3.4 Fake's `429`/`5xx` surface

`discord.py`'s real REST loop has fiddly retry semantics:

* **429 retry requires a `Via` header and a JSON-dict body.** Without
  the `Via` header `discord.py` interprets the 429 as a Cloudflare ban
  and surfaces it as a terminal `HTTPException` instead of retrying.
* **5xx retry only triggers for `{500, 502, 504, 524}`.** A scripted
  503 is terminal — by design in `discord.py`, mirrored exactly in
  the fake.
* The fake's `script_server_error` validates the status code at
  scripting time so a bad test fails loudly rather than silently
  hanging on the retry loop.

### 3.5 `pytest-asyncio` mode

The project sets `asyncio_mode = "auto"` in `pyproject.toml`. Async
test functions therefore do **not** need a `@pytest.mark.asyncio`
decorator. The smoke test takes advantage of this — every async test
in this file is just `async def test_...`.

### 3.6 What we explicitly chose *not* to fixture in Phase 0

* **A FastAPI test app.** The adapter doesn't exist yet (Phase 1).
  Bringing one up here would have invited premature coupling to
  routes that haven't been spec'd to the byte.
* **A real `osascript` exerciser.** Phase 2 owns the real sender
  (`OsascriptAppleScriptSender`); Phase 0's job is to prove the
  *seam* (`AppleScriptSender` protocol) is sound. The Phase 0 stub
  raises `NotImplementedError` so accidental use during a build-time
  test fails loudly.
* **A persistent SQLite store for messages.** That's the adapter's
  storage layer (§7.3); it lives in Phase 1.

## 4. Cross-fixture interop proof

`tests/fixtures/test_phase0_smoke.py` is the executable form of the
spec §9.0 checkpoint gate. Five tests, in this order:

1. `test_chat_db_loads_via_connector_read_path` — chat.db opens
   read-only via the planned connector path and the seeded DM thread is
   queryable through the four-way handle/chat join.
2. `test_fake_applescript_sender_records_invocation` — asserts the
   structural `isinstance(fake, AppleScriptSender)` substitution
   guarantee, then records a send and reconstructs the exact
   `osascript -e '...'` command line that would be invoked.
3. `test_fake_discord_gateway_identify_ready_and_message_create` — a
   real `discord.py` client completes IDENTIFY -> READY against the
   fake and a `MESSAGE_CREATE` payload round-trips into `on_message`.
4. `test_fake_discord_rest_records_channel_send` — `channel.send(...)`
   on a real `discord.py` `PartialMessageable` is intercepted by
   `FakeDiscordRest`, returns a hydrated `discord.Message`, and the
   outbound is recorded with the correct channel id and content.
5. `test_missing_chat_db_fixture_error_names_the_artifact` — pointing
   the read path at a non-existent file produces a SQLite error whose
   message names the missing file (the spec §9.0 error-handling
   criterion).

If any of those five fail, the Phase 0 gate is broken and Phase 1 / 2
work should stop until the regression is fixed.
