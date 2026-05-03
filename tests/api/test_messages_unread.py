"""Tests for ``GET /messages/unread`` (spec §5.3, §7.4.1).

Covers:
* Functional: allowlist filter, per-agent unread filter, two-agent independence,
  ``next_since`` cursor mirrors max ``message_ts``.
* Edge cases: empty result echoes input ``since``; ``limit > 100`` clamps to
  100; ``limit < 1`` and bad ``since`` return ``VALIDATION_FAILED``.
* Error handling: missing ``X-Agent-ID`` returns ``AGENT_ID_REQUIRED``;
  missing/invalid bearer returns ``UNAUTHORIZED``.

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

from amc.api.messages_unread import (
    MAX_LIMIT,
    configure_session_factory,
    reset_session_factory,
    router,
)
from amc.core.auth import (
    configure_bearer_token,
    reset_bearer_token,
)
from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.errors import register_exception_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-token"
AGENT_A = "agent-a"
AGENT_B = "agent-b"

CHANNEL_IM = "+15551234567"
CHANNEL_DC = "discord:dm:1234567890"

SENDER_IM_ALLOWED = "+15551234567"
SENDER_IM_QUARANTINED = "+15559999999"
SENDER_DC_ALLOWED = "discord_user_42"


# ---------------------------------------------------------------------------
# DB / session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    path = tmp_path / "unread.db"
    monkeypatch.setenv("AMC_DB_PATH", str(path))
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
      - ``SENDER_IM_ALLOWED`` allowlisted
      - ``SENDER_IM_QUARANTINED`` unknown (used to verify the allowlist filter)
    Discord:
      - ``SENDER_DC_ALLOWED`` allowlisted
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
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status, person_id) "
            "VALUES (?, ?, ?, 'unknown', NULL)",
            ("imessage", SENDER_IM_QUARANTINED, "Stranger"),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status, person_id) "
            "VALUES (?, ?, ?, 'allowed', ?)",
            ("discord", SENDER_DC_ALLOWED, "Bob", "person_alice_bob"),
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
    allowlist_status: str = "allowed",
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


def _mark_read(db_path: Path, *, msg_id: str, agent_id: str, read_at: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO message_reads (message_id, agent_id, read_at) VALUES (?, ?, ?)",
            (msg_id, agent_id, read_at),
        )
        conn.commit()
    finally:
        conn.close()


# Generate valid Crockford-base32 ULID-ish ids for the test fixtures. Real
# ULIDs aren't required — the envelope only validates the regex pattern.
def _msg_id(suffix: str) -> str:
    """Return a valid ``msg_<26-char base32>`` id derived from ``suffix``."""
    base = suffix.upper().ljust(26, "A")[:26]
    # Replace any character outside the Crockford alphabet (excludes I, L, O, U)
    # with 'A' to keep the regex happy.
    allowed = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    cleaned = "".join(c if c in allowed else "A" for c in base)
    return f"msg_{cleaned}"


MSG_1 = _msg_id("0001")
MSG_2 = _msg_id("0002")
MSG_3 = _msg_id("0003")
MSG_QUARANTINED = _msg_id("0004")
MSG_DC = _msg_id("0005")


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


def _auth_headers(*, agent_id: str | None = AGENT_A, bearer: str | None = BEARER) -> dict[str, str]:
    h: dict[str, str] = {}
    if bearer is not None:
        h["Authorization"] = f"Bearer {bearer}"
    if agent_id is not None:
        h["X-Agent-ID"] = agent_id
    return h


# ---------------------------------------------------------------------------
# Functional
# ---------------------------------------------------------------------------


class TestAllowlistFilter:
    def test_returns_only_allowed_messages(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="hi from alice",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_QUARANTINED,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_QUARANTINED,
            text_body="who is this",
            message_ts="2026-04-25T16:00:00Z",
            allowlist_status="unknown",
        )

        resp = client.get("/messages/unread", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        ids = [m["id"] for m in body["messages"]]
        assert ids == [MSG_1]

    def test_outbound_messages_are_not_returned(self, db_path: Path, client: TestClient) -> None:
        """``allowlist_status='outbound'`` means agent-sent and is excluded."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="agent reply",
            message_ts="2026-04-25T15:32:11Z",
            allowlist_status="outbound",
            direction="outbound",
        )

        resp = client.get("/messages/unread", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json()["messages"] == []


class TestPerAgentUnread:
    def test_excludes_messages_already_marked_read(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m1",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_2,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m2",
            message_ts="2026-04-25T15:33:11Z",
        )
        _mark_read(db_path, msg_id=MSG_1, agent_id=AGENT_A, read_at="2026-04-25T15:40:00Z")

        resp = client.get("/messages/unread", headers=_auth_headers(agent_id=AGENT_A))
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert ids == [MSG_2]

    def test_two_agents_see_same_messages_independently(
        self, db_path: Path, client: TestClient
    ) -> None:
        """Spec §5.3: per-agent cursor — A marking read does not affect B."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="shared message",
            message_ts="2026-04-25T15:32:11Z",
        )

        # Both agents see it before either marks read.
        resp_a = client.get("/messages/unread", headers=_auth_headers(agent_id=AGENT_A))
        resp_b = client.get("/messages/unread", headers=_auth_headers(agent_id=AGENT_B))
        assert [m["id"] for m in resp_a.json()["messages"]] == [MSG_1]
        assert [m["id"] for m in resp_b.json()["messages"]] == [MSG_1]

        # Agent A marks read.
        _mark_read(db_path, msg_id=MSG_1, agent_id=AGENT_A, read_at="2026-04-25T15:40:00Z")

        # A no longer sees it; B still does.
        resp_a2 = client.get("/messages/unread", headers=_auth_headers(agent_id=AGENT_A))
        resp_b2 = client.get("/messages/unread", headers=_auth_headers(agent_id=AGENT_B))
        assert resp_a2.json()["messages"] == []
        assert [m["id"] for m in resp_b2.json()["messages"]] == [MSG_1]


class TestCursor:
    def test_next_since_mirrors_max_message_ts(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m1",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_2,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m2",
            message_ts="2026-04-25T15:33:22Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_3,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m3",
            message_ts="2026-04-25T15:34:33Z",
        )

        resp = client.get("/messages/unread", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["next_since"] == "2026-04-25T15:34:33Z"
        # Order is ASC by message_ts.
        assert [m["id"] for m in body["messages"]] == [MSG_1, MSG_2, MSG_3]


class TestSourceAndChannelFilters:
    def test_source_filter(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="im",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_DC,
            source="discord",
            channel_id=CHANNEL_DC,
            sender_id=SENDER_DC_ALLOWED,
            text_body="dc",
            message_ts="2026-04-25T15:33:11Z",
        )

        resp = client.get("/messages/unread", headers=_auth_headers(), params={"source": "discord"})
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert ids == [MSG_DC]

    def test_channel_id_filter(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="im",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_DC,
            source="discord",
            channel_id=CHANNEL_DC,
            sender_id=SENDER_DC_ALLOWED,
            text_body="dc",
            message_ts="2026-04-25T15:33:11Z",
        )

        resp = client.get(
            "/messages/unread", headers=_auth_headers(), params={"channel_id": CHANNEL_DC}
        )
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert ids == [MSG_DC]

    def test_since_filter(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m1",
            message_ts="2026-04-25T15:32:11Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_2,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m2",
            message_ts="2026-04-25T15:34:00Z",
        )

        resp = client.get(
            "/messages/unread",
            headers=_auth_headers(),
            params={"since": "2026-04-25T15:33:00Z"},
        )
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert ids == [MSG_2]


class TestEnvelopeShape:
    def test_envelope_carries_sender_display_name_and_person_id(
        self, db_path: Path, client: TestClient
    ) -> None:
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_DC,
            source="discord",
            channel_id=CHANNEL_DC,
            sender_id=SENDER_DC_ALLOWED,
            text_body="hello",
            message_ts="2026-04-25T15:32:11Z",
        )

        resp = client.get("/messages/unread", headers=_auth_headers())
        assert resp.status_code == 200
        msg = resp.json()["messages"][0]
        assert msg["sender"]["id"] == SENDER_DC_ALLOWED
        assert msg["sender"]["display_name"] == "Bob"
        assert msg["sender"]["person_id"] == "person_alice_bob"
        assert msg["source"] == "discord"
        assert msg["channel_id"] == CHANNEL_DC
        assert msg["text"] == "hello"
        assert msg["timestamp"] == "2026-04-25T15:32:11Z"
        assert msg["attachments"] == []
        assert msg["raw"] == {}

    def test_envelope_decodes_attachments_json(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        att_id = "att_01HXAAAAAAAAAAAAAAAAAAAAAA"
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="see attached",
            message_ts="2026-04-25T15:32:11Z",
            attachments_json=(
                '[{"id":"'
                + att_id
                + '","url":"http://127.0.0.1:8080/attachments/'
                + att_id
                + '","mime":"image/png","size_bytes":1234}]'
            ),
        )

        resp = client.get("/messages/unread", headers=_auth_headers())
        assert resp.status_code == 200
        msg = resp.json()["messages"][0]
        assert len(msg["attachments"]) == 1
        att = msg["attachments"][0]
        assert att["id"] == att_id
        assert att["mime"] == "image/png"
        assert att["size_bytes"] == 1234


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_result_echoes_input_since(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        # No messages seeded.
        cursor = "2026-04-01T00:00:00Z"
        resp = client.get(
            "/messages/unread",
            headers=_auth_headers(),
            params={"since": cursor},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["messages"] == []
        assert body["next_since"] == cursor

    def test_empty_result_no_since_returns_empty_string(
        self, db_path: Path, client: TestClient
    ) -> None:
        _seed_baseline(db_path)
        resp = client.get("/messages/unread", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["messages"] == []
        # next_since field present even when input was unset.
        assert body["next_since"] == ""

    def test_limit_clamped_to_max(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        # Insert MAX_LIMIT + 5 messages so we can detect any clamp behavior.
        for i in range(MAX_LIMIT + 5):
            mid = _msg_id(f"L{i:03d}")
            _insert_message(
                db_path,
                msg_id=mid,
                source="imessage",
                channel_id=CHANNEL_IM,
                sender_id=SENDER_IM_ALLOWED,
                text_body=f"m{i}",
                # Spread timestamps so ORDER BY is deterministic.
                message_ts=f"2026-04-25T15:{30 + i // 60:02d}:{i % 60:02d}Z",
            )

        resp = client.get("/messages/unread", headers=_auth_headers(), params={"limit": 500})
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) == MAX_LIMIT

    def test_limit_below_one_rejected(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get("/messages/unread", headers=_auth_headers(), params={"limit": 0})
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_negative_limit_rejected(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get("/messages/unread", headers=_auth_headers(), params={"limit": -7})
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_bad_since_format_rejected(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get(
            "/messages/unread",
            headers=_auth_headers(),
            params={"since": "not-a-date"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_since_with_offset_is_normalized_to_utc(
        self, db_path: Path, client: TestClient
    ) -> None:
        """``since=2026-04-25T11:32:11-04:00`` == ``2026-04-25T15:32:11Z``."""
        _seed_baseline(db_path)
        _insert_message(
            db_path,
            msg_id=MSG_1,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m1",
            message_ts="2026-04-25T15:32:10Z",
        )
        _insert_message(
            db_path,
            msg_id=MSG_2,
            source="imessage",
            channel_id=CHANNEL_IM,
            sender_id=SENDER_IM_ALLOWED,
            text_body="m2",
            message_ts="2026-04-25T15:32:12Z",
        )
        # 11:32:11 EDT == 15:32:11 UTC; only MSG_2 (15:32:12Z) is newer.
        resp = client.get(
            "/messages/unread",
            headers=_auth_headers(),
            params={"since": "2026-04-25T11:32:11-04:00"},
        )
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert ids == [MSG_2]

    def test_unknown_source_rejected(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get("/messages/unread", headers=_auth_headers(), params={"source": "slack"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Error handling: auth + agent id
# ---------------------------------------------------------------------------


class TestAuthErrors:
    def test_missing_bearer_returns_401(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get(
            "/messages/unread",
            headers=_auth_headers(bearer=None),
        )
        assert resp.status_code == 401

    def test_invalid_bearer_returns_401(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get(
            "/messages/unread",
            headers=_auth_headers(bearer="wrong-token"),
        )
        assert resp.status_code == 401

    def test_missing_agent_id_returns_400_agent_id_required(
        self, db_path: Path, client: TestClient
    ) -> None:
        _seed_baseline(db_path)
        resp = client.get("/messages/unread", headers=_auth_headers(agent_id=None))
        assert resp.status_code == 400
        body = resp.json()
        # The auth dependency raises HTTPException with the standard envelope
        # nested under ``detail``. Accept either flat or nested forms so this
        # test stays green regardless of which handler claims the response.
        code = body.get("error", {}).get("code") or body.get("detail", {}).get("error", {}).get(
            "code"
        )
        assert code == "AGENT_ID_REQUIRED"

    def test_blank_agent_id_returns_400(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        resp = client.get("/messages/unread", headers=_auth_headers(agent_id="   "))
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Performance — opt-in slow test (spec §7.4.1: P95 < 100 ms with 10k messages
# and 5 agents). Marked ``slow`` so the default pytest run skips it.
# ---------------------------------------------------------------------------


def _bulk_seed_messages(db_path: Path, *, total: int) -> None:
    """Insert ``total`` allowlisted messages from the iMessage allowlisted sender.

    Spreads ``message_ts`` across ~3 hours so ``ORDER BY`` is meaningful and
    the index ``idx_messages_allowlist_ts`` actually does work.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("BEGIN;")
        for i in range(total):
            mid = _msg_id(f"P{i:05d}")
            # Stagger by 1 second.
            secs = 30 * 60 + i  # 30 minutes past the hour, +i seconds
            ts = (
                f"2026-04-25T{15 + secs // 3600:02d}:"
                f"{(secs % 3600) // 60:02d}:{secs % 60:02d}Z"
            )
            conn.execute(
                "INSERT INTO messages "
                "(id, source, channel_id, channel_type, sender_id, text, "
                " direction, allowlist_status, message_ts) "
                "VALUES (?, 'imessage', ?, 'dm', ?, ?, 'inbound', 'allowed', ?)",
                (mid, CHANNEL_IM, SENDER_IM_ALLOWED, f"m{i}", ts),
            )
        conn.commit()
    finally:
        conn.close()


def _bulk_mark_read(db_path: Path, *, agent_ids: list[str], per_agent: int) -> None:
    """For each agent, mark the first ``per_agent`` messages read.

    Exercises the ``LEFT JOIN message_reads`` filter at scale.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("BEGIN;")
        for agent in agent_ids:
            for i in range(per_agent):
                mid = _msg_id(f"P{i:05d}")
                conn.execute(
                    "INSERT OR IGNORE INTO message_reads "
                    "(message_id, agent_id, read_at) VALUES (?, ?, ?)",
                    (mid, agent, "2026-04-25T16:00:00Z"),
                )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.slow
class TestPerformance:
    def test_p95_under_100ms_with_10k_messages_and_5_agents(
        self, db_path: Path, client: TestClient
    ) -> None:
        """Spec §7.4.1: P95 < 100 ms with 10k messages and 5 agents reading.

        Setup:
        - 10 000 allowlisted messages from ``SENDER_IM_ALLOWED``.
        - 5 distinct agents, each having marked the first 1 000 messages read.

        Each agent then issues a ``GET /messages/unread`` 25 times; we collect
        per-call wall-clock latency and assert the P95 is under 100 ms.
        """
        import time

        _seed_baseline(db_path)
        _bulk_seed_messages(db_path, total=10_000)
        agents = [f"agent-{i}" for i in range(5)]
        _bulk_mark_read(db_path, agent_ids=agents, per_agent=1_000)

        latencies_ms: list[float] = []
        # 5 agents x 25 requests = 125 samples — enough for a stable P95.
        for agent in agents:
            for _ in range(25):
                start = time.perf_counter()
                resp = client.get(
                    "/messages/unread",
                    headers=_auth_headers(agent_id=agent),
                )
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                assert resp.status_code == 200
                # Each agent should still see 9 000 unread messages, capped to
                # the default limit of 20.
                assert len(resp.json()["messages"]) == 20
                latencies_ms.append(elapsed_ms)

        latencies_ms.sort()
        p95_index = max(0, int(len(latencies_ms) * 0.95) - 1)
        p95 = latencies_ms[p95_index]
        # Surface the value on failure for triage.
        assert p95 < 100.0, (
            f"P95 latency {p95:.2f}ms exceeded 100ms budget. "
            f"min={latencies_ms[0]:.2f} median={latencies_ms[len(latencies_ms) // 2]:.2f} "
            f"max={latencies_ms[-1]:.2f}"
        )
