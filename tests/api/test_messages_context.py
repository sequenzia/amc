"""Tests for ``GET /messages/context`` (spec §5.4 / §7.4.3).

Coverage walks the acceptance-criteria checklist for task #32:

* Functional
    - Chronological order (target + before + after)
    - Outbound messages are included (agent's own replies)
    - Quarantined messages are excluded from neighbors
    - Channel with fewer than ``before`` older messages returns what's available
* Edge Cases
    - Target is quarantined → 404 ``MESSAGE_NOT_FOUND``
    - ``before=0`` and ``after=0`` returns just the target
    - ``before > 50`` is rejected as ``VALIDATION_FAILED`` per the Pydantic clamp
* Error Handling
    - Unknown ``around_message_id`` → 404 ``MESSAGE_NOT_FOUND``

The test suite spins up a fresh in-test FastAPI app per test, applies the
``alembic upgrade head`` migration to a tmp_path SQLite file, seeds rows via
plain ``sqlite3`` (event-loop-friendly), and exercises the route via
``fastapi.testclient.TestClient``.
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

from amg.api.messages_context import router as context_router
from amg.core.auth import configure_bearer_token, reset_bearer_token
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.errors import register_exception_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-token"
AGENT_ID = "claude-code"
AUTH_HEADERS = {
    "Authorization": f"Bearer {BEARER}",
    "X-Agent-ID": AGENT_ID,
}

CHANNEL = "+15551234567"
OTHER_CHANNEL = "+15559999999"

# Sender ids used across tests. Allowlisted = visible; quarantined = unknown.
SENDER_ALICE = "+15551234567"
SENDER_QUARANTINED = "+15550000000"

# Pre-baked ULID-prefixed message ids. The Envelope pattern requires Crockford
# base32 (no I, L, O, U) and exactly 26 chars after ``msg_``.
# Layout per id: 6-char prefix ``01HXYZ`` + 19 ``A`` chars + 1 distinct suffix.
MSG_T0 = "msg_01HXYZAAAAAAAAAAAAAAAAAAA0"
MSG_T1 = "msg_01HXYZAAAAAAAAAAAAAAAAAAA1"
MSG_T3 = "msg_01HXYZAAAAAAAAAAAAAAAAAAA3"
MSG_T4 = "msg_01HXYZAAAAAAAAAAAAAAAAAAA4"
MSG_T5 = "msg_01HXYZAAAAAAAAAAAAAAAAAAA5"
MSG_OUTBOUND = "msg_01HXYZAAAAAAAAAAAAAAAAAAAB"
MSG_QUARANTINED = "msg_01HXYZAAAAAAAAAAAAAAAAAAAQ"
MSG_OTHER_CHANNEL = "msg_01HXYZAAAAAAAAAAAAAAAAAAAC"
UNKNOWN_MSG_ID = "msg_01HXYZAAAAAAAAAAAAAAAAAAAF"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    path = tmp_path / "context.db"
    monkeypatch.setenv("AMG_DB_PATH", str(path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    return path


def _seed(db_path: Path) -> None:
    """Insert the canonical test fixture: two channels, two senders, several
    messages with known ``message_ts`` ordering.

    Layout in the primary channel ``CHANNEL`` (oldest → newest):

    - T0: inbound, allowed (Alice)
    - T1: inbound, allowed (Alice)
    - T2: outbound, allowlist_status='outbound' (agent reply)
    - T3: inbound, allowed (Alice)            ← typical "target"
    - T4: inbound, allowed (Alice)
    - T5: inbound, allowed (Alice)
    - QUARANTINED: inbound, unknown (between T3 and T4 in timestamp)

    Plus one message in ``OTHER_CHANNEL`` so we can prove the target's
    ``channel_id`` is enforced.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        # Channels
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, 'dm')",
            ("imessage", CHANNEL),
        )
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, 'dm')",
            ("imessage", OTHER_CHANNEL),
        )
        # Senders
        conn.execute(
            "INSERT INTO senders "
            "(source, sender_id, display_name, allowlist_status, person_id) "
            "VALUES (?, ?, ?, 'allowed', ?)",
            ("imessage", SENDER_ALICE, "Alice", "person_alice"),
        )
        conn.execute(
            "INSERT INTO senders "
            "(source, sender_id, display_name, allowlist_status) "
            "VALUES (?, ?, ?, 'unknown')",
            ("imessage", SENDER_QUARANTINED, "Unknown Caller"),
        )

        rows = [
            (MSG_T0, SENDER_ALICE, "first", "inbound", "allowed", "2026-04-25T15:00:00Z", CHANNEL),
            (MSG_T1, SENDER_ALICE, "second", "inbound", "allowed", "2026-04-25T15:00:10Z", CHANNEL),
            (
                MSG_OUTBOUND,
                SENDER_ALICE,
                "agent reply",
                "outbound",
                "outbound",
                "2026-04-25T15:00:15Z",
                CHANNEL,
            ),
            (MSG_T3, SENDER_ALICE, "target", "inbound", "allowed", "2026-04-25T15:00:20Z", CHANNEL),
            # Quarantined sits between T3 and T4 in time.
            (
                MSG_QUARANTINED,
                SENDER_QUARANTINED,
                "spam",
                "inbound",
                "unknown",
                "2026-04-25T15:00:25Z",
                CHANNEL,
            ),
            (MSG_T4, SENDER_ALICE, "fourth", "inbound", "allowed", "2026-04-25T15:00:30Z", CHANNEL),
            (MSG_T5, SENDER_ALICE, "fifth", "inbound", "allowed", "2026-04-25T15:00:40Z", CHANNEL),
            (
                MSG_OTHER_CHANNEL,
                SENDER_ALICE,
                "elsewhere",
                "inbound",
                "allowed",
                "2026-04-25T15:00:00Z",
                OTHER_CHANNEL,
            ),
        ]
        for mid, sid, text, direction, allow, ts, ch in rows:
            conn.execute(
                "INSERT INTO messages "
                "(id, source, channel_id, channel_type, sender_id, text, "
                " direction, allowlist_status, message_ts) "
                "VALUES (?, 'imessage', ?, 'dm', ?, ?, ?, ?, ?)",
                (mid, ch, sid, text, direction, allow, ts),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def app(db_path: Path) -> Iterator[FastAPI]:
    """Build an in-test FastAPI app wired to the migrated tmp_path DB."""
    configure_bearer_token(BEARER)

    application = FastAPI()
    register_exception_handlers(application)

    engine = create_engine_from_env(db_path_override=db_path)
    factory = create_session_factory(engine)
    application.state.session_factory = factory

    application.include_router(context_router)

    try:
        yield application
    finally:
        reset_bearer_token()
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture
def client(app: FastAPI, db_path: Path) -> TestClient:
    _seed(db_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ids(payload: dict) -> list[str]:
    return [m["id"] for m in payload["messages"]]


# ---------------------------------------------------------------------------
# Functional
# ---------------------------------------------------------------------------


class TestFunctional:
    def test_returns_chronological_target_with_before_and_after(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
                "before": 2,
                "after": 2,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # before=2 → T1 + outbound, target=T3, after=2 → T4 + T5.
        assert _ids(body) == [MSG_T1, MSG_OUTBOUND, MSG_T3, MSG_T4, MSG_T5]
        # Spot-check the envelope shape.
        first = body["messages"][0]
        assert first["channel_id"] == CHANNEL
        assert first["sender"]["id"] == SENDER_ALICE
        assert first["sender"]["display_name"] == "Alice"
        assert first["timestamp"].endswith("Z")

    def test_includes_outbound_messages(self, client: TestClient) -> None:
        """Outbound messages (agent replies) appear in context."""
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
                "before": 5,
                "after": 0,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        ids = _ids(resp.json())
        assert MSG_OUTBOUND in ids
        # And the outbound row carries direction='outbound' on the wire.
        wire_outbound = next(m for m in resp.json()["messages"] if m["id"] == MSG_OUTBOUND)
        assert wire_outbound["direction"] == "outbound"

    def test_excludes_quarantined_neighbors(self, client: TestClient) -> None:
        """``allowlist_status='unknown'`` rows are filtered out of neighbors."""
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
                "before": 0,
                "after": 5,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        ids = _ids(resp.json())
        assert MSG_QUARANTINED not in ids
        # Target + the two allowed messages newer than it.
        assert ids == [MSG_T3, MSG_T4, MSG_T5]

    def test_fewer_messages_than_before_returns_what_is_available(self, client: TestClient) -> None:
        """No padding when there aren't enough older messages."""
        # T0 has zero messages older than it; ask for 5 before.
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T0,
                "before": 5,
                "after": 0,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert _ids(resp.json()) == [MSG_T0]

    def test_default_before_and_after_are_five(self, client: TestClient) -> None:
        """Omitted ``before``/``after`` default to 5."""
        resp = client.get(
            "/messages/context",
            params={"channel_id": CHANNEL, "around_message_id": MSG_T3},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        ids = _ids(resp.json())
        # Before T3 (allowed only): T0, T1, OUTBOUND. Target: T3. After: T4, T5.
        assert ids == [MSG_T0, MSG_T1, MSG_OUTBOUND, MSG_T3, MSG_T4, MSG_T5]

    def test_messages_from_other_channels_are_excluded(self, client: TestClient) -> None:
        """Neighbor scan is scoped to the target's channel."""
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
                "before": 50,
                "after": 50,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert MSG_OTHER_CHANNEL not in _ids(resp.json())


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_quarantined_target_returns_404(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_QUARANTINED,
                "before": 5,
                "after": 5,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "MESSAGE_NOT_FOUND"

    def test_before_zero_after_zero_returns_just_target(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
                "before": 0,
                "after": 0,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert _ids(resp.json()) == [MSG_T3]

    def test_before_above_50_is_validation_failed(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
                "before": 51,
                "after": 5,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_after_above_50_is_validation_failed(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
                "before": 5,
                "after": 51,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_negative_before_is_validation_failed(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
                "before": -1,
                "after": 5,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_target_in_other_channel_returns_404(self, client: TestClient) -> None:
        """Target exists, but ``channel_id`` query disagrees → 404."""
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_OTHER_CHANNEL,
                "before": 5,
                "after": 5,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MESSAGE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_unknown_message_id_returns_404(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": UNKNOWN_MSG_ID,
                "before": 5,
                "after": 5,
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "MESSAGE_NOT_FOUND"

    def test_missing_bearer_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
            },
            headers={"X-Agent-ID": AGENT_ID},
        )
        assert resp.status_code == 401
        # ``require_bearer`` raises ``HTTPException(detail={"error": {...}})``;
        # the canonical flatten lives in :func:`register_exception_handlers`
        # only for ``AMGError``, so the auth envelope still wraps under
        # ``detail`` (spec-conformant after task #27 re-translation, but
        # already correct in shape today).
        assert resp.json()["detail"]["error"]["code"] == "UNAUTHORIZED"

    def test_missing_agent_id_returns_400(self, client: TestClient) -> None:
        resp = client.get(
            "/messages/context",
            params={
                "channel_id": CHANNEL,
                "around_message_id": MSG_T3,
            },
            headers={"Authorization": f"Bearer {BEARER}"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "AGENT_ID_REQUIRED"

    def test_missing_required_query_params_returns_400(self, client: TestClient) -> None:
        resp = client.get("/messages/context", headers=AUTH_HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
