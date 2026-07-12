"""Tests for ``POST /messages/mark_read`` (spec §5.3, §7.4.4, OQ-1).

Covers:

* Functional: first call marks N ids, second call (same ids) returns same N
  (idempotent UPSERT), different agents produce independent rows.
* Edge cases: empty ``message_ids`` returns 0, non-existent ids are still
  counted (per OQ-1) and silently skipped at the storage layer.
* Error handling: missing ``X-Agent-ID`` -> 400 ``AGENT_ID_REQUIRED``,
  missing/invalid body -> 400 ``VALIDATION_FAILED``.

DB strategy mirrors ``tests/core/test_idempotency.py``: alembic upgrade on a
tmp_path SQLite file, async sessionmaker built per-test, exception handlers
registered so the standard error envelope (``{"error": {"code": ...}}``)
appears in the response.
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

from amg.api.messages_mark_read import (
    MarkReadRequest,
    configure_session_factory,
    reset_session_factory,
    router,
)
from amg.core.auth import configure_bearer_token, reset_bearer_token
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.errors import register_exception_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

VALID_TOKEN = "tok-mark-read-test-deadbeefdeadbeef"
AGENT_A = "agent-alpha"
AGENT_B = "agent-bravo"

# Valid Crockford-base32 ULID-suffixed message ids (26 chars after `msg_`).
MSG_1 = "msg_01HXYZ1234567890ABCDEFGHJK"
MSG_2 = "msg_01HXYZ1234567890ABCDEFGHJM"
MSG_3 = "msg_01HXYZ1234567890ABCDEFGHJN"
MSG_NONEXISTENT = "msg_01HXYZ9999999999ZZZZZZZZZZ"


# ---------------------------------------------------------------------------
# DB / app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""

    path = tmp_path / "mark_read.db"
    monkeypatch.setenv("AMG_DB_PATH", str(path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    return path


def _seed_messages(db_path: Path, message_ids: list[str]) -> None:
    """Insert one channel + sender + N message rows so FKs resolve."""

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) "
            "VALUES ('imessage', '+15551234567', 'dm')"
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES ('imessage', '+15551234567', 'Alice', 'allowed')"
        )
        for mid in message_ids:
            conn.execute(
                "INSERT INTO messages "
                "(id, source, channel_id, channel_type, sender_id, text, "
                " direction, allowlist_status, message_ts) "
                "VALUES (:id, 'imessage', '+15551234567', 'dm', '+15551234567', "
                "        'hi', 'inbound', 'allowed', '2026-05-03T12:00:00Z')",
                {"id": mid},
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def session_factory(db_path: Path) -> Iterator[async_sessionmaker]:
    """Async session factory bound to the migrated tmp_path DB.

    Synchronous fixture so the engine is not pinned to pytest-asyncio's
    event loop; ``TestClient`` spins up its own loop per request.
    """

    engine = create_engine_from_env(db_path_override=db_path)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture
def app(session_factory: async_sessionmaker) -> Iterator[FastAPI]:
    """Mount a minimal FastAPI app with bearer + the mark_read router."""

    configure_bearer_token(VALID_TOKEN)
    configure_session_factory(session_factory)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    try:
        yield app
    finally:
        reset_session_factory()
        reset_bearer_token()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _auth_headers(*, agent_id: str | None = AGENT_A) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {VALID_TOKEN}"}
    if agent_id is not None:
        headers["X-Agent-ID"] = agent_id
    return headers


def _read_marks(db_path: Path) -> list[tuple[str, str, str]]:
    """Return all rows from ``message_reads`` ordered by (message_id, agent_id)."""

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT message_id, agent_id, read_at FROM message_reads ORDER BY message_id, agent_id"
        ).fetchall()
        return list(rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


class TestMarkReadRequest:
    def test_accepts_list_of_ids(self) -> None:
        req = MarkReadRequest(message_ids=[MSG_1, MSG_2])
        assert req.message_ids == [MSG_1, MSG_2]

    def test_accepts_empty_list(self) -> None:
        req = MarkReadRequest(message_ids=[])
        assert req.message_ids == []

    def test_missing_field_raises(self) -> None:
        with pytest.raises(ValueError):
            MarkReadRequest()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Functional
# ---------------------------------------------------------------------------


class TestFunctional:
    def test_first_call_marks_n_messages_and_returns_marked_count(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_messages(db_path, [MSG_1, MSG_2, MSG_3])

        resp = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={"message_ids": [MSG_1, MSG_2, MSG_3]},
        )

        assert resp.status_code == 200
        assert resp.json() == {"marked_count": 3}

        rows = _read_marks(db_path)
        assert len(rows) == 3
        assert {(r[0], r[1]) for r in rows} == {
            (MSG_1, AGENT_A),
            (MSG_2, AGENT_A),
            (MSG_3, AGENT_A),
        }
        # read_at is non-empty
        assert all(r[2] for r in rows)

    def test_second_call_same_ids_returns_same_count_idempotent(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_messages(db_path, [MSG_1, MSG_2])

        first = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={"message_ids": [MSG_1, MSG_2]},
        )
        assert first.status_code == 200
        assert first.json() == {"marked_count": 2}
        first_rows = _read_marks(db_path)
        assert len(first_rows) == 2
        first_read_ats = {(r[0], r[1]): r[2] for r in first_rows}

        second = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={"message_ids": [MSG_1, MSG_2]},
        )
        assert second.status_code == 200
        assert second.json() == {"marked_count": 2}

        second_rows = _read_marks(db_path)
        # Still exactly two rows (no duplicates).
        assert len(second_rows) == 2
        # PK is unchanged. read_at may have advanced (UPSERT refreshes it).
        second_read_ats = {(r[0], r[1]): r[2] for r in second_rows}
        assert set(first_read_ats.keys()) == set(second_read_ats.keys())

    def test_different_agents_marking_same_message_produces_independent_rows(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_messages(db_path, [MSG_1])

        resp_a = client.post(
            "/messages/mark_read",
            headers=_auth_headers(agent_id=AGENT_A),
            json={"message_ids": [MSG_1]},
        )
        resp_b = client.post(
            "/messages/mark_read",
            headers=_auth_headers(agent_id=AGENT_B),
            json={"message_ids": [MSG_1]},
        )

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        assert resp_a.json() == {"marked_count": 1}
        assert resp_b.json() == {"marked_count": 1}

        rows = _read_marks(db_path)
        assert len(rows) == 2
        assert {(r[0], r[1]) for r in rows} == {
            (MSG_1, AGENT_A),
            (MSG_1, AGENT_B),
        }

    def test_duplicate_ids_in_one_call_dedupe_before_counting(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_messages(db_path, [MSG_1])

        resp = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={"message_ids": [MSG_1, MSG_1, MSG_1]},
        )

        assert resp.status_code == 200
        assert resp.json() == {"marked_count": 1}
        rows = _read_marks(db_path)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_message_ids_returns_marked_count_zero(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        resp = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={"message_ids": []},
        )
        assert resp.status_code == 200
        assert resp.json() == {"marked_count": 0}
        # No rows touched.
        assert _read_marks(db_path) == []

    def test_nonexistent_ids_are_counted_but_not_persisted_per_oq1(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        """Per OQ-1 resolution: counted in marked_count, silently skipped in DB."""

        _seed_messages(db_path, [MSG_1])

        resp = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={"message_ids": [MSG_1, MSG_NONEXISTENT]},
        )
        assert resp.status_code == 200
        # Both ids counted (OQ-1 resolution: total submitted, deduped).
        assert resp.json() == {"marked_count": 2}

        rows = _read_marks(db_path)
        # Only the existing id has a persisted row — FK violation skipped.
        assert len(rows) == 1
        assert rows[0][0] == MSG_1
        assert rows[0][1] == AGENT_A

    def test_all_nonexistent_ids_return_count_but_no_rows(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        resp = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={"message_ids": [MSG_NONEXISTENT]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"marked_count": 1}
        assert _read_marks(db_path) == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_missing_x_agent_id_returns_400_agent_id_required(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/messages/mark_read",
            headers=_auth_headers(agent_id=None),
            json={"message_ids": [MSG_1]},
        )
        assert resp.status_code == 400
        body = resp.json()
        # Bearer auth is wired to HTTPException (pre-#27 envelope is at
        # body["detail"]["error"]["code"] until task #35 lands). Either
        # location is acceptable as long as the canonical code is present.
        code = body.get("error", {}).get("code") or body.get("detail", {}).get("error", {}).get(
            "code"
        )
        assert code == "AGENT_ID_REQUIRED"

    def test_blank_x_agent_id_returns_400_agent_id_required(
        self,
        client: TestClient,
    ) -> None:
        headers = _auth_headers()
        headers["X-Agent-ID"] = "   "
        resp = client.post(
            "/messages/mark_read",
            headers=headers,
            json={"message_ids": [MSG_1]},
        )
        assert resp.status_code == 400
        body = resp.json()
        code = body.get("error", {}).get("code") or body.get("detail", {}).get("error", {}).get(
            "code"
        )
        assert code == "AGENT_ID_REQUIRED"

    def test_missing_message_ids_returns_400_validation_failed(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_message_ids_wrong_type_returns_400_validation_failed(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/messages/mark_read",
            headers=_auth_headers(),
            json={"message_ids": "not-a-list"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_missing_bearer_returns_401_unauthorized(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/messages/mark_read",
            headers={"X-Agent-ID": AGENT_A},
            json={"message_ids": [MSG_1]},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    def test_uncalled_session_factory_raises_at_request_time(
        self,
        db_path: Path,  # noqa: ARG002 — needed to bring up alembic
    ) -> None:
        """If startup forgets to configure the factory the route raises."""

        # Build an app WITHOUT calling configure_session_factory.
        configure_bearer_token(VALID_TOKEN)
        reset_session_factory()
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router)

        try:
            with TestClient(app, raise_server_exceptions=False) as test_client:
                resp = test_client.post(
                    "/messages/mark_read",
                    headers=_auth_headers(),
                    json={"message_ids": [MSG_1]},
                )
            # The catch-all handler renders INTERNAL_ERROR.
            assert resp.status_code == 500
            body = resp.json()
            assert body["error"]["code"] == "INTERNAL_ERROR"
        finally:
            reset_bearer_token()
