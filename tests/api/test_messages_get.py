"""Tests for ``GET /messages/{id}`` (spec §7.4.2).

Coverage map:

Functional:
* Allowed inbound message → 200 + envelope.
* Quarantined (``allowlist_status='unknown'``) inbound → 404
  ``MESSAGE_NOT_FOUND`` (NOT 403 — §7.4.2 collapses not-visible into
  not-found).
* Missing message id → 404 ``MESSAGE_NOT_FOUND``.

Edge cases:
* Outbound message (``direction='outbound'``,
  ``allowlist_status='outbound'``) → 200; only inbound ``unknown`` is hidden.

Error handling:
* Bearer header missing → 401 ``UNAUTHORIZED``.
* Bearer header wrong → 401 ``UNAUTHORIZED``.
* ``X-Agent-ID`` missing → 400 ``AGENT_ID_REQUIRED``.

Routing collision regression:
* The path-param regex on ``MessageId`` ensures ``GET /messages/unread``
  (sibling task #30) does NOT match this route.

Test infrastructure:
* tmp_path SQLite + ``alembic upgrade head`` (mirrors
  ``tests/core/test_idempotency.py``).
* In-test FastAPI app fixture mounting the router.
* ``configure_session_factory`` for the DB and ``configure_bearer_token``
  for auth — same pattern as elsewhere in the codebase.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from amg.api.messages_get import (
    configure_session_factory,
    reset_session_factory,
    router,
)
from amg.core.auth import configure_bearer_token, reset_bearer_token
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.errors import register_exception_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER_TOKEN = "test-bearer-token-xyz"
AGENT_ID = "claude-code"

# Valid Crockford-base32 ULIDs (26 chars; alphabet excludes I, L, O, U).
MSG_ID_ALLOWED = "msg_01HQXR9F3TQX3MT5N8K0JVB7G2"
MSG_ID_QUARANTINED = "msg_01HQXR9G7VWX5NF8P2T0HJBC4Y"
MSG_ID_OUTBOUND = "msg_01HQXR9HBYZ7QHKBR4VC5KMD8N"
MSG_ID_MISSING = "msg_01HQXR9JDA28SJMCS6XE7NPF9P"

CHANNEL_ID = "+15551234567"
CHANNEL_ID_OUT = "+15559998888"

SENDER_ALLOWED = "+15550001111"
SENDER_QUARANTINED = "+15550002222"
SENDER_AGENT = "agent"


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    path = tmp_path / "messages_get.db"
    monkeypatch.setenv("AMG_DB_PATH", str(path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    return path


@pytest.fixture
def session_factory(db_path: Path) -> Iterator[async_sessionmaker]:
    """Async session factory bound to the migrated tmp_path DB.

    Synchronous fixture so the engine isn't pinned to pytest-asyncio's event
    loop; FastAPI ``TestClient`` spins up its own loop per request.
    """
    engine = create_engine_from_env(db_path_override=db_path)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture
def seeded_db(db_path: Path) -> Path:
    """Seed channels, senders, and messages directly via sqlite3.

    Uses the sync driver so we don't fight event-loop ownership with
    ``TestClient`` later. Three messages are seeded:

    1. Allowed inbound (``MSG_ID_ALLOWED``) — sender allowlist 'allowed'.
    2. Quarantined inbound (``MSG_ID_QUARANTINED``) — sender allowlist
       'unknown'.
    3. Outbound (``MSG_ID_OUTBOUND``) — message ``allowlist_status='outbound'``.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        # channels
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, ?)",
            ("imessage", CHANNEL_ID, "dm"),
        )
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, ?)",
            ("imessage", CHANNEL_ID_OUT, "dm"),
        )

        # senders
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, "
            "                     allowlist_status, person_id, "
            "                     first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "imessage",
                SENDER_ALLOWED,
                "Allowed Person",
                "allowed",
                "person-allowed",
                "2026-05-03T12:00:00.000Z",
                "2026-05-03T12:00:00.000Z",
            ),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, "
            "                     allowlist_status, person_id, "
            "                     first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "imessage",
                SENDER_QUARANTINED,
                SENDER_QUARANTINED,
                "unknown",
                None,
                "2026-05-03T12:00:00.000Z",
                "2026-05-03T12:00:00.000Z",
            ),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, "
            "                     allowlist_status, person_id, "
            "                     first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "imessage",
                SENDER_AGENT,
                "Agent",
                "allowed",
                None,
                "2026-05-03T12:00:00.000Z",
                "2026-05-03T12:00:00.000Z",
            ),
        )

        # Messages — note channel for outbound is distinct so we don't
        # need to give the agent a sender row tied to CHANNEL_ID.
        conn.execute(
            "INSERT INTO messages (id, source, channel_id, channel_type, "
            "                      sender_id, text, direction, "
            "                      allowlist_status, message_ts, "
            "                      attachments_json, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                MSG_ID_ALLOWED,
                "imessage",
                CHANNEL_ID,
                "dm",
                SENDER_ALLOWED,
                "hello allowed",
                "inbound",
                "allowed",
                "2026-05-03T12:30:00.000Z",
                None,
                None,
            ),
        )
        conn.execute(
            "INSERT INTO messages (id, source, channel_id, channel_type, "
            "                      sender_id, text, direction, "
            "                      allowlist_status, message_ts, "
            "                      attachments_json, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                MSG_ID_QUARANTINED,
                "imessage",
                CHANNEL_ID,
                "dm",
                SENDER_QUARANTINED,
                "hello quarantined",
                "inbound",
                "unknown",
                "2026-05-03T12:31:00.000Z",
                None,
                None,
            ),
        )
        conn.execute(
            "INSERT INTO messages (id, source, channel_id, channel_type, "
            "                      sender_id, text, direction, "
            "                      allowlist_status, message_ts, "
            "                      attachments_json, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                MSG_ID_OUTBOUND,
                "imessage",
                CHANNEL_ID_OUT,
                "dm",
                SENDER_AGENT,
                "agent reply",
                "outbound",
                "outbound",
                "2026-05-03T12:32:00.000Z",
                None,
                None,
            ),
        )
        conn.commit()
    return db_path


# ---------------------------------------------------------------------------
# App / client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(
    seeded_db: Path,  # noqa: ARG001  — ensures DB is seeded before app runs
    session_factory: async_sessionmaker,
) -> Iterator[FastAPI]:
    """Mount a minimal FastAPI app exposing the messages_get router."""
    configure_bearer_token(BEARER_TOKEN)
    configure_session_factory(session_factory)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)

    try:
        yield app
    finally:
        reset_bearer_token()
        reset_session_factory()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "X-Agent-ID": AGENT_ID,
    }


# ---------------------------------------------------------------------------
# Functional tests
# ---------------------------------------------------------------------------


class TestAllowed:
    def test_allowed_message_returns_envelope(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.get(f"/messages/{MSG_ID_ALLOWED}", headers=auth_headers)
        assert resp.status_code == 200, resp.text

        body = resp.json()
        assert body["id"] == MSG_ID_ALLOWED
        assert body["source"] == "imessage"
        assert body["channel_id"] == CHANNEL_ID
        assert body["channel_type"] == "dm"
        assert body["text"] == "hello allowed"
        assert body["direction"] == "inbound"
        assert body["sender"] == {
            "id": SENDER_ALLOWED,
            "display_name": "Allowed Person",
            "person_id": "person-allowed",
        }
        assert body["attachments"] == []
        assert body["reply_to"] is None
        # Timestamp should be canonical Z-suffix RFC 3339.
        assert body["timestamp"].endswith("Z")
        assert body["raw"] == {}


class TestQuarantined:
    def test_quarantined_message_returns_404_not_403(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.get(f"/messages/{MSG_ID_QUARANTINED}", headers=auth_headers)
        # MUST be 404 (not 403) — quarantined collapses to not-found.
        assert resp.status_code == 404
        body = resp.json()
        assert body == {
            "error": {
                "code": "MESSAGE_NOT_FOUND",
                "message": f"Message {MSG_ID_QUARANTINED!r} not found.",
            }
        }


class TestMissing:
    def test_unknown_id_returns_404(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.get(f"/messages/{MSG_ID_MISSING}", headers=auth_headers)
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "MESSAGE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Edge case: outbound visibility
# ---------------------------------------------------------------------------


class TestOutbound:
    def test_outbound_message_is_visible(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.get(f"/messages/{MSG_ID_OUTBOUND}", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == MSG_ID_OUTBOUND
        assert body["direction"] == "outbound"
        assert body["text"] == "agent reply"


# ---------------------------------------------------------------------------
# Error handling: auth
# ---------------------------------------------------------------------------


def _error_code(body: dict) -> str:
    """Extract the canonical error code from either envelope shape.

    The route-side ``AMGError`` handler emits ``{"error": {"code": ...}}``
    directly. The auth dependencies (``amg.core.auth``) still raise stock
    ``HTTPException(detail=...)`` which FastAPI nests under ``"detail"`` — task
    #27/#35 will flatten it. Tolerate both shapes so this test file does not
    break when that flattening lands.
    """
    if "error" in body:
        return body["error"]["code"]
    if "detail" in body and isinstance(body["detail"], dict):
        return body["detail"]["error"]["code"]
    raise AssertionError(f"Unexpected error body shape: {body!r}")


class TestAuth:
    def test_missing_bearer_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            f"/messages/{MSG_ID_ALLOWED}",
            headers={"X-Agent-ID": AGENT_ID},
        )
        assert resp.status_code == 401
        assert _error_code(resp.json()) == "UNAUTHORIZED"

    def test_invalid_bearer_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            f"/messages/{MSG_ID_ALLOWED}",
            headers={
                "Authorization": "Bearer not-the-real-token",
                "X-Agent-ID": AGENT_ID,
            },
        )
        assert resp.status_code == 401
        assert _error_code(resp.json()) == "UNAUTHORIZED"

    def test_missing_agent_id_returns_400(self, client: TestClient) -> None:
        resp = client.get(
            f"/messages/{MSG_ID_ALLOWED}",
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        )
        assert resp.status_code == 400
        assert _error_code(resp.json()) == "AGENT_ID_REQUIRED"

    def test_blank_agent_id_returns_400(self, client: TestClient) -> None:
        resp = client.get(
            f"/messages/{MSG_ID_ALLOWED}",
            headers={
                "Authorization": f"Bearer {BEARER_TOKEN}",
                "X-Agent-ID": "   ",
            },
        )
        assert resp.status_code == 400
        assert _error_code(resp.json()) == "AGENT_ID_REQUIRED"


# ---------------------------------------------------------------------------
# Routing-collision regression
# ---------------------------------------------------------------------------


class TestPathParamRegex:
    """The ``MessageId`` regex on ``{message_id}`` must reject non-ULID slugs.

    This guarantees sibling routes like ``/messages/unread`` (#30) and
    ``/messages/context`` (#32) won't be shadowed by the ``{id}`` route at
    app-mount time.
    """

    @pytest.mark.parametrize(
        "slug",
        [
            "unread",
            "context",
            "mark_read",
            "quarantine",
            "send",
            "msg_too_short",
            "wrong_prefix_01HQXR9F3TQX3MT5N8K0JVB7G2",
        ],
    )
    def test_non_ulid_slug_does_not_match_route(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        slug: str,
    ) -> None:
        resp = client.get(f"/messages/{slug}", headers=auth_headers)
        # Either 404 (no route matched at all) or 400 (validation failed). The
        # critical point: NEVER 200 with an envelope body — the route did not
        # claim the path as a valid ``{id}``.
        assert resp.status_code != 200
