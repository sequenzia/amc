"""End-to-end iMessage round-trip integration test (task #54).

Spec references: §5.1 (iMessage inbound poll path), §5.2 (iMessage outbound
AppleScript path), §7.4.1 (``GET /messages/unread``), §7.4.5
(``POST /messages/send``), §7.4.11 (header table).

Scenario walked end-to-end:

1. A writable copy of the Phase 0 chat.db fixture is materialised in a
   temp directory.
2. A real :class:`amc.connectors.imessage.connector.ImessageConnector`
   is wired against that temp chat.db, with a
   :class:`tests.fakes.applescript.FakeAppleScriptSender` injected for
   the outbound path. The full :mod:`amc.app` adapter is booted on top
   of a freshly-migrated SQLite store.
3. A new ``message`` row (allowlisted handle, ROWID past the seed
   ``MAX(message.ROWID)``) is appended to the temp chat.db using
   ``sqlite3`` directly -- mimicking what Messages.app does when a new
   iMessage arrives (one ``INSERT INTO message`` plus one
   ``INSERT INTO chat_message_join``).
4. The test polls ``GET /messages/unread`` until the corresponding
   envelope appears (2 s ceiling -- the poll loop runs at 50 ms in
   tests, so a single cycle is more than enough).
5. The ``channel_id`` returned by the envelope is fed straight into
   ``POST /messages/send`` to drive the outbound path.
6. Two assertions seal the round-trip:
   * The :class:`FakeAppleScriptSender` recorded exactly one call with
     the canonical ``chat.guid`` (``iMessage;-;<handle>``) and the
     outbound text body.
   * The ``messages`` table holds exactly one ``direction='outbound'``
     row pointing at the same channel, so the persistence side of the
     send path also fired.

The test reuses helpers from :mod:`tests.e2e._helpers` (task #74) for
schema bootstrap, allowlist construction, chat.db row injection, and
read-only DB introspection so the wiring stays small.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from amc.api.messages_send import (
    configure_imessage_connector,
    configure_rate_limiter,
    reset_imessage_connector,
    reset_rate_limiter,
)
from amc.api.messages_send import (
    configure_session_factory as configure_send_session_factory,
)
from amc.api.messages_send import (
    reset_session_factory as reset_send_session_factory,
)
from amc.api.messages_unread import (
    configure_session_factory as configure_unread_session_factory,
)
from amc.api.messages_unread import (
    reset_session_factory as reset_unread_session_factory,
)
from amc.app import build_app
from amc.connectors.imessage.connector import ImessageConnector
from amc.connectors.imessage.reader import ChatDbReader
from amc.core.auth import configure_bearer_token, reset_bearer_token
from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.envelope import Source
from amc.core.idempotency import (
    IdempotencyStore,
    configure_idempotency_store,
    reset_idempotency_store,
)
from amc.core.message_sink import MessageSink
from amc.core.rate_limit import RateLimiter
from tests.e2e._helpers import (
    append_chat_db_inbound_row,
    apply_alembic_head,
    build_inmemory_allowlist,
    list_messages_for_assertion,
)
from tests.fakes.applescript import FakeAppleScriptSender
from tests.fixtures.build_chat_db import (
    ALLOWLISTED_HANDLE,
    CHAT_GUID,
    ensure_chat_db,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-e2e-imessage-roundtrip"  # noqa: S105 -- test fixture
AGENT_ID = "agent-e2e-imessage-roundtrip"

# ROWID for the injected inbound row. The Phase 0 fixture seeds 6 rows
# (ROWIDs 1..6); on a fresh install the connector seeds its cursor to
# MAX(ROWID)=6 so the seeded backlog is NOT replayed. Any value > 6
# is therefore "new" from the poller's perspective.
INJECTED_ROWID = 100
INJECTED_TEXT = "ping from imessage e2e roundtrip"

# Aggressive poll interval so the iMessage connector reacts within ~50 ms.
TEST_POLL_INTERVAL = 0.05

# Bound for how long we'll wait for the inbound envelope to materialise.
# The brief specifies a 2 s ceiling.
INBOUND_POLL_TIMEOUT = 2.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def src_chat_db_path() -> Path:
    """Materialise the Phase 0 chat.db once per module."""
    return ensure_chat_db()


@pytest.fixture
def chat_db_copy(src_chat_db_path: Path, tmp_path: Path) -> Path:
    """Per-test writable copy of the chat.db fixture."""
    import shutil

    dst = tmp_path / "chat.db"
    shutil.copy2(src_chat_db_path, dst)
    return dst


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Fresh SQLite file with the full Alembic migrations applied."""
    db_path = tmp_path / "amc-imessage-roundtrip.db"
    monkeypatch.setenv("AMC_DB_PATH", str(db_path))
    apply_alembic_head(ALEMBIC_INI, db_path)
    yield db_path


@pytest.fixture(autouse=True)
def _reset_module_globals() -> Iterator[None]:
    """Make sure no prior test leaked module-level state into this one."""
    reset_bearer_token()
    reset_send_session_factory()
    reset_unread_session_factory()
    reset_rate_limiter()
    reset_imessage_connector()
    reset_idempotency_store()
    yield
    reset_bearer_token()
    reset_send_session_factory()
    reset_unread_session_factory()
    reset_rate_limiter()
    reset_imessage_connector()
    reset_idempotency_store()


# ---------------------------------------------------------------------------
# Live-system bundle
# ---------------------------------------------------------------------------


class _LiveSystem:
    """Hand-rolled context manager bundling every component for cleanup."""

    __slots__ = (
        "engine",
        "session_factory",
        "sink",
        "fake_sender",
        "imessage_connector",
        "rate_limiter",
        "idempotency_store",
        "fastapi_client",
        "_client_cm",
    )

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_factory: async_sessionmaker,
        sink: MessageSink,
        fake_sender: FakeAppleScriptSender,
        imessage_connector: ImessageConnector,
        rate_limiter: RateLimiter,
        idempotency_store: IdempotencyStore,
        fastapi_client: TestClient,
        client_cm: contextlib.AbstractContextManager[TestClient],
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.sink = sink
        self.fake_sender = fake_sender
        self.imessage_connector = imessage_connector
        self.rate_limiter = rate_limiter
        self.idempotency_store = idempotency_store
        self.fastapi_client = fastapi_client
        self._client_cm = client_cm


async def _start_live_system(
    *,
    db_path: Path,
    chat_db_path: Path,
) -> _LiveSystem:
    """Wire up + start every component for the round-trip test.

    The wiring deliberately mirrors what an eventual ``amc.app`` lifespan
    hook will do once that landed: build the engine, build the sink +
    iMessage connector, register everything on the messages_send /
    messages_unread routers, and start the connector's background poller.
    """
    engine = create_engine_from_env(db_path_override=db_path)
    session_factory = create_session_factory(engine)

    allowlist = build_inmemory_allowlist(
        [
            (
                Source.IMESSAGE,
                ALLOWLISTED_HANDLE,
                "Allowlisted Alice (iMessage)",
                "person_alice",
            ),
        ]
    )

    sink = MessageSink(
        session_factory=session_factory,
        allowlist=allowlist,
        webhook_config=None,  # Not exercising the webhook path in this test.
    )

    fake_sender = FakeAppleScriptSender()
    reader = ChatDbReader(chat_db_path)
    imessage_connector = ImessageConnector(
        reader=reader,
        sender=fake_sender,
        sink=sink,
        allowlist=allowlist,
        session_factory=session_factory,
        attachment_store=None,
        poll_interval_seconds=TEST_POLL_INTERVAL,
    )
    await imessage_connector.start()

    # ---- Adapter HTTP wiring ------------------------------------------------
    configure_bearer_token(BEARER)
    configure_send_session_factory(session_factory)
    configure_unread_session_factory(session_factory)

    rate_limiter = RateLimiter(rps=100.0, burst=100)
    configure_rate_limiter(rate_limiter)

    idempotency_store = IdempotencyStore(session_factory=session_factory)
    configure_idempotency_store(idempotency_store)

    configure_imessage_connector(imessage_connector)

    app = build_app(bootstrap=False)
    client_cm = TestClient(app, raise_server_exceptions=False)
    client = client_cm.__enter__()

    return _LiveSystem(
        engine=engine,
        session_factory=session_factory,
        sink=sink,
        fake_sender=fake_sender,
        imessage_connector=imessage_connector,
        rate_limiter=rate_limiter,
        idempotency_store=idempotency_store,
        fastapi_client=client,
        client_cm=client_cm,
    )


async def _stop_live_system(live: _LiveSystem) -> None:
    """Tear everything down cleanly so background tasks don't leak."""
    with contextlib.suppress(Exception):
        await live.imessage_connector.stop()

    with contextlib.suppress(Exception):
        live._client_cm.__exit__(None, None, None)  # noqa: SLF001 -- intentional cleanup

    with contextlib.suppress(Exception):
        await live.engine.dispose()


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------


async def _poll_unread_for_text(
    client: TestClient,
    *,
    expected_text: str,
    timeout: float,
) -> dict[str, Any]:
    """Poll ``GET /messages/unread`` until an envelope with ``expected_text`` appears.

    Returns the envelope dict (the first match). Raises ``TimeoutError``
    if no match arrives within ``timeout`` seconds.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last_seen: list[dict[str, Any]] = []
    while asyncio.get_running_loop().time() < deadline:
        resp = client.get(
            "/messages/unread",
            headers={
                "Authorization": f"Bearer {BEARER}",
                "X-Agent-ID": AGENT_ID,
            },
        )
        assert resp.status_code == 200, (
            f"GET /messages/unread failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()
        last_seen = body.get("messages", [])
        for envelope in last_seen:
            if envelope.get("text") == expected_text:
                return envelope
        await asyncio.sleep(0.02)
    raise TimeoutError(
        f"envelope with text {expected_text!r} did not appear in /messages/unread "
        f"within {timeout}s; last response had {len(last_seen)} envelope(s)"
    )


def _read_outbound_rows(db_path: Path) -> list[dict[str, Any]]:
    """Return every ``direction='outbound'`` row, oldest first."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, source, channel_id, text, direction, delivery_status "
            "FROM messages WHERE direction = 'outbound' "
            "ORDER BY message_ts ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_imessage_inbound_envelope_then_outbound_send_roundtrip(
    fresh_db: Path,
    chat_db_copy: Path,
) -> None:
    """End-to-end iMessage round-trip.

    Inject a new chat.db row, watch it surface via ``GET /messages/unread``,
    fire the resulting ``channel_id`` back through ``POST /messages/send``,
    and assert both the AppleScript fake AND the persisted outbound row
    reflect the send.
    """
    live = await _start_live_system(
        db_path=fresh_db,
        chat_db_path=chat_db_copy,
    )

    try:
        # ----------------------------------------------------------------
        # 1. Inject an inbound row that mimics Messages.app:
        #    INSERT INTO message + INSERT INTO chat_message_join.
        # ----------------------------------------------------------------
        # ``handle_id=1`` and ``chat_id=1`` come from the Phase 0 fixture's
        # allowlisted DM thread (``ALLOWLISTED_HANDLE`` / ``CHAT_GUID``).
        append_chat_db_inbound_row(
            chat_db_copy,
            rowid=INJECTED_ROWID,
            text_value=INJECTED_TEXT,
            handle_id=1,
            is_from_me=0,
            chat_id=1,
        )

        # ----------------------------------------------------------------
        # 2. Wait for the connector to pick it up and the sink to persist
        #    the envelope (visible via /messages/unread).
        # ----------------------------------------------------------------
        envelope = await _poll_unread_for_text(
            live.fastapi_client,
            expected_text=INJECTED_TEXT,
            timeout=INBOUND_POLL_TIMEOUT,
        )

        # Sanity on the surfaced envelope.
        assert envelope["source"] == "imessage"
        assert envelope["channel_id"] == ALLOWLISTED_HANDLE, (
            f"expected channel_id={ALLOWLISTED_HANDLE!r} (E.164 handle); "
            f"got {envelope['channel_id']!r}"
        )
        assert envelope["direction"] == "inbound"
        assert envelope["text"] == INJECTED_TEXT

        channel_id = envelope["channel_id"]

        # ----------------------------------------------------------------
        # 3. Drive the outbound path: POST /messages/send with that
        #    channel_id.
        # ----------------------------------------------------------------
        outbound_text = "pong from e2e roundtrip"
        send_resp = live.fastapi_client.post(
            "/messages/send",
            headers={
                "Authorization": f"Bearer {BEARER}",
                "X-Agent-ID": AGENT_ID,
                "Content-Type": "application/json",
            },
            json={"channel_id": channel_id, "text": outbound_text},
        )
        assert send_resp.status_code == 200, (
            f"POST /messages/send failed: {send_resp.status_code} {send_resp.text}"
        )
        send_body = send_resp.json()
        assert send_body["message_id"].startswith("msg_")

        # ----------------------------------------------------------------
        # 4. The FakeAppleScriptSender recorded the outbound call with the
        #    canonical chat.guid + the body we sent.
        # ----------------------------------------------------------------
        live.fake_sender.assert_call_count(1)
        call = live.fake_sender.calls[0]
        assert call.chat_guid == CHAT_GUID, (
            f"expected chat_guid={CHAT_GUID!r} (pinned by inbound row's "
            f"channels.metadata_json); got {call.chat_guid!r}"
        )
        assert call.text == outbound_text
        assert call.attachment_path is None

        # ----------------------------------------------------------------
        # 5. The outbound row landed in the messages table with
        #    direction='outbound'.
        # ----------------------------------------------------------------
        outbound_rows = _read_outbound_rows(fresh_db)
        assert len(outbound_rows) == 1, (
            f"expected exactly one outbound row; got {len(outbound_rows)}: "
            f"{outbound_rows}"
        )
        outbound = outbound_rows[0]
        assert outbound["source"] == "imessage"
        assert outbound["channel_id"] == channel_id
        assert outbound["direction"] == "outbound"
        assert outbound["text"] == outbound_text
        assert outbound["delivery_status"] == "sent"
        assert outbound["id"] == send_body["message_id"]

        # The inbound envelope is also still in messages (round-trip
        # leaves both rows visible).
        all_rows = list_messages_for_assertion(fresh_db)
        inbound_rows = [r for r in all_rows if r["direction"] == "inbound"]
        assert any(r["text"] == INJECTED_TEXT for r in inbound_rows), (
            f"inbound row missing from messages after round-trip: {all_rows}"
        )

    finally:
        await _stop_live_system(live)
