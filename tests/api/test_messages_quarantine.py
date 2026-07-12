"""Tests for ``GET /messages/quarantine`` (spec §5.7, §5.9, §7.4.8).

Covers:
* Functional: only ``allowlist_status='unknown'`` rows returned; `since`
  pagination filters older rows; ``next_since`` reflects max returned
  ``message_ts``; ordering is ``message_ts DESC``.
* Edge cases: empty result returns ``messages: []`` and echoes input ``since``;
  ``limit > 100`` clamps to 100; ``limit < 1`` rejected; bad ``since`` rejected.
* Error handling: missing/invalid bearer returns 401. ``X-Agent-ID`` is **not**
  required (operator endpoint).

The tests mount the route on an in-test FastAPI app, register the standard
exception handlers, point a fresh ``alembic upgrade head`` SQLite DB at the
session factory, and seed messages directly via SQL.
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

from amg.api.messages_quarantine import (
    MAX_LIMIT,
    configure_session_factory,
    reset_session_factory,
    router,
)
from amg.core.auth import (
    configure_bearer_token,
    reset_bearer_token,
)
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.errors import register_exception_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-token"

CHANNEL_IM = "+15551234567"
CHANNEL_DC = "discord:dm:1234567890"

SENDER_IM_ALLOWED = "+15551234567"
SENDER_IM_QUARANTINED = "+15559999999"
SENDER_IM_QUARANTINED_2 = "+15558888888"
SENDER_DC_QUARANTINED = "discord_user_99"


# ---------------------------------------------------------------------------
# DB / session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    path = tmp_path / "quarantine.db"
    monkeypatch.setenv("AMG_DB_PATH", str(path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    return path


@pytest.fixture
def session_factory(db_path: Path) -> Iterator[async_sessionmaker]:
    """Session factory bound to the migrated tmp_path DB.

    Synchronous fixture so the engine isn't pinned to pytest-asyncio's loop;
    FastAPI ``TestClient`` spins up its own loop per request.
    """
    engine = create_engine_from_env(db_path_override=db_path)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def reset_globals() -> Iterator[None]:
    """Reset module-level config caches between tests."""
    reset_bearer_token()
    reset_session_factory()
    yield
    reset_bearer_token()
    reset_session_factory()


# ---------------------------------------------------------------------------
# Seeding helpers — synchronous sqlite3 so we can populate before the app boots
# ---------------------------------------------------------------------------


def _seed_baseline(db_path: Path) -> None:
    """Insert channels + senders that messages will reference.

    iMessage:
      - ``SENDER_IM_ALLOWED`` allowlisted (used to verify the allowlist filter
        excludes 'allowed' rows from the quarantine listing)
      - ``SENDER_IM_QUARANTINED`` and ``SENDER_IM_QUARANTINED_2`` unknown
    Discord:
      - ``SENDER_DC_QUARANTINED`` unknown
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, 'dm')",
            ("imessage", CHANNEL_IM),
        )
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, 'dm')",
            ("discord", CHANNEL_DC),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES (?, ?, ?, 'allowed')",
            ("imessage", SENDER_IM_ALLOWED, "Alice"),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES (?, ?, ?, 'unknown')",
            ("imessage", SENDER_IM_QUARANTINED, "Stranger 1"),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES (?, ?, ?, 'unknown')",
            ("imessage", SENDER_IM_QUARANTINED_2, "Stranger 2"),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES (?, ?, ?, 'unknown')",
            ("discord", SENDER_DC_QUARANTINED, "Discord Stranger"),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_message(
    db_path: Path,
    *,
    msg_id: str,
    source: str,
    channel_id: str,
    sender_id: str,
    text_body: str,
    message_ts: str,
    allowlist_status: str = "unknown",
    direction: str = "inbound",
    attachments_json: str | None = None,
    raw_json: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts, attachments_json, raw_json) "
            "VALUES (?, ?, ?, 'dm', ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                source,
                channel_id,
                sender_id,
                text_body,
                direction,
                allowlist_status,
                message_ts,
                attachments_json,
                raw_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# Generate valid Crockford-base32 ULID-ish ids for the test fixtures. Real
# ULIDs aren't required — the envelope only validates the regex pattern.
def _msg_id(suffix: str) -> str:
    """Return a valid ``msg_<26-char base32>`` id derived from ``suffix``."""
    base = suffix.upper().ljust(26, "A")[:26]
    allowed = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    cleaned = "".join(c if c in allowed else "A" for c in base)
    return f"msg_{cleaned}"


MSG_Q1 = _msg_id("Q001")
MSG_Q2 = _msg_id("Q002")
MSG_Q3 = _msg_id("Q003")
MSG_ALLOWED = _msg_id("A001")
MSG_DC_Q = _msg_id("DCQ1")


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def client(session_factory: async_sessionmaker) -> Iterator[TestClient]:
    configure_bearer_token(BEARER)
    configure_session_factory(session_factory)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)

    with TestClient(app) as c:
        yield c


def _auth_headers(*, bearer: str | None = BEARER) -> dict[str, str]:
    """Build request headers — quarantine endpoint takes ONLY bearer (no X-Agent-ID)."""
    h: dict[str, str] = {}
    if bearer is not None:
        h["Authorization"] = f"Bearer {bearer}"
    return h


# ---------------------------------------------------------------------------
# Functional
# ---------------------------------------------------------------------------


class TestQuarantineFilter:
    def test_returns_only_unknown_status_messages(self, db_path: Path, client: TestClient) -> None:
        """Allowlisted messages must NOT appear; quarantined must."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_ALLOWED,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="hi from alice (allowlisted)",
            message_ts="2026-04-25T15:32:11Z",
            allowlist_status="allowed",
        )
        _insert_message(
            db_path,
            msg_id=MSG_Q1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="who is this",
            message_ts="2026-04-25T16:00:00Z",
            allowlist_status="unknown",
        )

        resp = client.get("/messages/quarantine", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        ids = [m["id"] for m in body["messages"]]
        # Allowlisted message must NOT appear; only quarantined one does.
        assert ids == [MSG_Q1]

    def test_outbound_messages_are_not_returned(self, db_path: Path, client: TestClient) -> None:
        """``allowlist_status='outbound'`` is agent-sent and not quarantined."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_ALLOWED,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="agent reply",
            message_ts="2026-04-25T15:32:11Z",
            allowlist_status="outbound",
            direction="outbound",
        )

        resp = client.get("/messages/quarantine", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json()["messages"] == []

    def test_returns_quarantined_across_sources(self, db_path: Path, client: TestClient) -> None:
        """Quarantine listing is global — both iMessage and Discord rows surface."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_Q1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="im quarantine",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_DC_Q,
            source="discord",
            channel_id=CHANNEL_DC,
            sender_id=SENDER_DC_QUARANTINED,
            text_body="dc quarantine",
            message_ts="2026-04-25T15:33:11Z",
        )

        resp = client.get("/messages/quarantine", headers=_auth_headers())
        assert resp.status_code == 200
        ids = sorted(m["id"] for m in resp.json()["messages"])
        assert ids == sorted([MSG_Q1, MSG_DC_Q])


class TestOrdering:
    def test_results_are_ordered_message_ts_desc(self, db_path: Path, client: TestClient) -> None:
        """Newest first — operator review starts at the latest unknown sender."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_Q1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="oldest",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_Q2,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="middle",
            message_ts="2026-04-25T15:33:22Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_Q3,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED_2,
            text_body="newest",
            message_ts="2026-04-25T15:34:33Z",
        )

        resp = client.get("/messages/quarantine", headers=_auth_headers())
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert ids == [MSG_Q3, MSG_Q2, MSG_Q1]


class TestPagination:
    def test_since_filters_older_messages(self, db_path: Path, client: TestClient) -> None:
        """`since` returns only rows with ``message_ts > since``."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_Q1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="old",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_Q2,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="new",
            message_ts="2026-04-25T15:34:00Z",
        )

        resp = client.get(
            "/messages/quarantine",
            headers=_auth_headers(),
            params={"since": "2026-04-25T15:33:00Z"},
        )
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert ids == [MSG_Q2]

    def test_next_since_reflects_max_message_ts(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_Q1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="m1",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_Q2,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="m2",
            message_ts="2026-04-25T15:33:22Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_Q3,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED_2,
            text_body="m3",
            message_ts="2026-04-25T15:34:33Z",
        )

        resp = client.get("/messages/quarantine", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        # next_since == max(message_ts) of returned rows.
        assert body["next_since"] == "2026-04-25T15:34:33Z"

    def test_since_with_offset_normalized_to_utc(self, db_path: Path, client: TestClient) -> None:
        """``since=2026-04-25T11:32:11-04:00`` == ``2026-04-25T15:32:11Z``."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_Q1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="m1",
            message_ts="2026-04-25T15:32:10Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_Q2,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="m2",
            message_ts="2026-04-25T15:32:12Z",
        )
        # 11:32:11 EDT == 15:32:11 UTC; only MSG_Q2 (15:32:12Z) is newer.
        resp = client.get(
            "/messages/quarantine",
            headers=_auth_headers(),
            params={"since": "2026-04-25T11:32:11-04:00"},
        )
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert ids == [MSG_Q2]


class TestEnvelopeShape:
    def test_envelope_carries_sender_display_name(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_Q1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="hello",
            message_ts="2026-04-25T15:32:11Z",
        )

        resp = client.get("/messages/quarantine", headers=_auth_headers())
        assert resp.status_code == 200
        msg = resp.json()["messages"][0]
        assert msg["sender"]["id"] == SENDER_IM_QUARANTINED
        assert msg["sender"]["display_name"] == "Stranger 1"
        assert msg["sender"]["person_id"] is None
        assert msg["source"] == "imessage"
        assert msg["channel_id"] == CHANNEL_IM
        assert msg["text"] == "hello"
        assert msg["timestamp"] == "2026-04-25T15:32:11Z"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_result_returns_empty_messages(self, db_path: Path, client: TestClient) -> None:
        """No quarantined messages -> ``messages: []`` and empty next_since."""
        _seed_baseline(db_path)
        # Only an allowlisted message exists; quarantine listing is empty.
        _insert_message(
            db_path,
            msg_id=MSG_ALLOWED,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="hi",
            message_ts="2026-04-25T15:32:11Z",
            allowlist_status="allowed",
        )

        resp = client.get("/messages/quarantine", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["messages"] == []
        assert body["next_since"] == ""

    def test_empty_result_echoes_input_since(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        cursor = "2026-04-01T00:00:00Z"
        resp = client.get(
            "/messages/quarantine",
            headers=_auth_headers(),
            params={"since": cursor},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["messages"] == []
        assert body["next_since"] == cursor

    def test_limit_clamped_to_max(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        for i in range(MAX_LIMIT + 5):
            mid = _msg_id(f"L{i:03d}")
            _insert_message(
                db_path,
                msg_id=mid,
                source="imessage",
                channel_id=CHANNEL_IM,
                sender_id=SENDER_IM_QUARANTINED,
                text_body=f"m{i}",
                message_ts=f"2026-04-25T15:{30 + i // 60:02d}:{i % 60:02d}Z",
            )

        resp = client.get(
            "/messages/quarantine",
            headers=_auth_headers(),
            params={"limit": 500},
        )
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) == MAX_LIMIT

    def test_limit_below_one_rejected(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get(
            "/messages/quarantine",
            headers=_auth_headers(),
            params={"limit": 0},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_negative_limit_rejected(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get(
            "/messages/quarantine",
            headers=_auth_headers(),
            params={"limit": -1},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_bad_since_format_rejected(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get(
            "/messages/quarantine",
            headers=_auth_headers(),
            params={"since": "not-a-date"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Error handling: auth
# ---------------------------------------------------------------------------


class TestAuthErrors:
    def test_missing_bearer_returns_401(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get("/messages/quarantine", headers=_auth_headers(bearer=None))
        assert resp.status_code == 401

    def test_invalid_bearer_returns_401(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get(
            "/messages/quarantine",
            headers=_auth_headers(bearer="wrong-token"),
        )
        assert resp.status_code == 401

    def test_no_x_agent_id_required(self, db_path: Path, client: TestClient) -> None:
        """Quarantine review is global: no X-Agent-ID header is needed.

        The unread endpoint requires it; this one explicitly does not.
        """
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_Q1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="q",
            message_ts="2026-04-25T15:32:11Z",
        )
        # Headers contain only bearer, no X-Agent-ID.
        resp = client.get("/messages/quarantine", headers=_auth_headers())
        assert resp.status_code == 200
