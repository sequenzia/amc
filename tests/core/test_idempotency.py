"""Tests for ``amg.core.idempotency`` (spec §5.2 / §7.3.3 / §7.4.5).

Coverage:

* Body normalization: order-independent JSON, deterministic SHA-256.
* Store-level: lookup miss, persist + lookup hit, expiry, sweeper, race-loser.
* End-to-end FastAPI dependency:
    - Fresh key → 200 + row persisted.
    - Replay same body → cached response + ``Idempotency-Replayed: true``.
    - Replay different body → 422 ``IDEMPOTENCY_KEY_REUSE``.
    - Missing ``Idempotency-Key`` → handler runs every time (no idempotency).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.errors import register_exception_handlers
from amg.core.idempotency import (
    DEFAULT_TTL,
    REPLAY_HEADER,
    IdempotencyContext,
    IdempotencyStore,
    configure_idempotency_store,
    hash_body,
    idempotent,
    normalize_body,
    register_idempotency_handlers,
    reset_idempotency_store,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


# ---------------------------------------------------------------------------
# Clock fake
# ---------------------------------------------------------------------------


class _FakeClock:
    """Manually advanced UTC clock for deterministic expiry tests."""

    __slots__ = ("now",)

    def __init__(self, start: datetime | None = None) -> None:
        self.now: datetime = start or datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""

    path = tmp_path / "idem.db"
    monkeypatch.setenv("AMG_DB_PATH", str(path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    return path


@pytest.fixture
def session_factory(db_path: Path) -> Iterator[async_sessionmaker]:
    """Async session factory bound to the migrated tmp_path DB.

    Synchronous fixture so the engine isn't pinned to pytest-asyncio's event
    loop; FastAPI ``TestClient`` spins up its own loop per request and would
    otherwise see "Future attached to a different loop" errors.
    """

    engine = create_engine_from_env(db_path_override=db_path)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        # Best-effort dispose; the underlying aiosqlite connections are
        # reaped when the engine is GC'd if dispose() can't run cleanly here.
        import asyncio
        import contextlib

        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def store(
    session_factory: async_sessionmaker,
    clock: _FakeClock,
) -> IdempotencyStore:
    return IdempotencyStore(session_factory, time_provider=clock)


# ---------------------------------------------------------------------------
# Body normalization
# ---------------------------------------------------------------------------


class TestNormalizeBody:
    def test_empty_body_normalizes_to_empty(self) -> None:
        assert normalize_body(b"") == b""

    def test_key_order_independent(self) -> None:
        a = b'{"text":"hi","channel_id":"+1"}'
        b = b'{"channel_id":"+1","text":"hi"}'
        assert normalize_body(a) == normalize_body(b)
        assert hash_body(a) == hash_body(b)

    def test_whitespace_stripped(self) -> None:
        a = b'{"a": 1, "b": 2}'
        b = b'{"a":1,"b":2}'
        assert normalize_body(a) == normalize_body(b)
        assert hash_body(a) == hash_body(b)

    def test_nested_keys_also_sorted(self) -> None:
        a = b'{"outer":{"x":1,"y":2},"top":1}'
        b = b'{"top":1,"outer":{"y":2,"x":1}}'
        assert hash_body(a) == hash_body(b)

    def test_different_values_produce_different_hash(self) -> None:
        a = b'{"text":"hi"}'
        b = b'{"text":"bye"}'
        assert hash_body(a) != hash_body(b)

    def test_non_json_body_falls_back_to_raw(self) -> None:
        # Malformed JSON: still hashable, just byte-for-byte.
        raw = b"not-json"
        assert normalize_body(raw) == raw

    def test_hash_is_hex_sha256(self) -> None:
        h = hash_body(b'{"a":1}')
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Store: lookup / persist / expiry / sweeper
# ---------------------------------------------------------------------------


class TestStoreLookupPersist:
    async def test_lookup_miss_returns_none(self, store: IdempotencyStore) -> None:
        assert await store.lookup("missing-key") is None

    async def test_persist_then_lookup_hit(self, store: IdempotencyStore) -> None:
        cached = await store.persist(
            key="k1",
            request_hash="hash-a",
            response_status=200,
            response_body='{"message_id":"msg_1"}',
        )
        assert cached.request_hash == "hash-a"
        assert cached.response_status == 200
        assert cached.response_body == '{"message_id":"msg_1"}'

        hit = await store.lookup("k1")
        assert hit is not None
        assert hit.request_hash == "hash-a"
        assert hit.response_status == 200

    async def test_persist_is_no_op_on_duplicate_key(
        self,
        store: IdempotencyStore,
    ) -> None:
        """Second writer with the same key sees the first writer's row."""

        await store.persist(
            key="k2",
            request_hash="hash-first",
            response_status=200,
            response_body='{"first":true}',
        )
        # Second persist with a *different* body must not clobber the row.
        cached = await store.persist(
            key="k2",
            request_hash="hash-second",
            response_status=500,
            response_body='{"second":true}',
        )
        assert cached.request_hash == "hash-first"
        assert cached.response_body == '{"first":true}'
        assert cached.response_status == 200


class TestStoreExpiry:
    async def test_lookup_returns_none_after_24h(
        self,
        store: IdempotencyStore,
        clock: _FakeClock,
    ) -> None:
        await store.persist(
            key="exp-1",
            request_hash="h",
            response_status=200,
            response_body="{}",
        )
        # Just inside the TTL window.
        clock.advance(DEFAULT_TTL - timedelta(seconds=1))
        assert await store.lookup("exp-1") is not None

        # Push past expiry.
        clock.advance(timedelta(seconds=2))
        assert await store.lookup("exp-1") is None

    async def test_sweeper_deletes_expired_rows_only(
        self,
        store: IdempotencyStore,
        clock: _FakeClock,
    ) -> None:
        # Row 1: persisted at t0, will expire at t0 + 24h.
        await store.persist(
            key="old",
            request_hash="h1",
            response_status=200,
            response_body="{}",
        )

        # Advance 23 h, persist row 2 (expires at t+47h).
        clock.advance(timedelta(hours=23))
        await store.persist(
            key="fresh",
            request_hash="h2",
            response_status=200,
            response_body="{}",
        )

        # Push to t0 + 25h → row 1 expired, row 2 still alive.
        clock.advance(timedelta(hours=2))
        deleted = await store.sweep_expired()
        assert deleted == 1
        assert await store.lookup("old") is None
        assert await store.lookup("fresh") is not None

    async def test_sweeper_returns_zero_when_nothing_expired(
        self,
        store: IdempotencyStore,
    ) -> None:
        await store.persist(
            key="recent",
            request_hash="h",
            response_status=200,
            response_body="{}",
        )
        assert await store.sweep_expired() == 0


# ---------------------------------------------------------------------------
# Concurrent INSERT race
# ---------------------------------------------------------------------------


class TestConcurrentRace:
    async def test_two_parallel_writers_converge_on_first_writer_response(
        self,
        store: IdempotencyStore,
    ) -> None:
        """Two coroutines persist the same key → both see the same cached row.

        SQLite serializes the writes; whichever lands first wins. The other's
        ``persist`` call returns the winner's body so the loser can swap its
        response transparently.
        """

        async def writer(body_marker: str) -> str:
            cached = await store.persist(
                key="race-key",
                request_hash=f"hash-{body_marker}",
                response_status=200,
                response_body=json.dumps({"writer": body_marker}),
            )
            return cached.response_body

        results = await asyncio.gather(writer("A"), writer("B"))
        # Both calls observe the SAME canonical body — exactly one writer's
        # marker. We don't care which one won, only that they agree.
        assert results[0] == results[1]
        winning_body = json.loads(results[0])
        assert winning_body["writer"] in {"A", "B"}


# ---------------------------------------------------------------------------
# End-to-end FastAPI integration
# ---------------------------------------------------------------------------


# Module-level dependency annotation — keeps ``Depends(...)`` out of the
# route handler's argument defaults (ruff B008) and matches the FastAPI
# ``Annotated`` style.
_IdemDep = Annotated[IdempotencyContext | None, Depends(idempotent("send"))]


@pytest.fixture
def app(store: IdempotencyStore) -> Iterator[FastAPI]:
    """Mount a minimal app exercising the dependency on a real route."""

    configure_idempotency_store(store)
    app = FastAPI()
    register_exception_handlers(app)
    register_idempotency_handlers(app)

    # Counter so tests can prove the handler ran (or didn't).
    call_count = {"n": 0}

    @app.post("/send")
    async def send(payload: dict, idem: _IdemDep) -> dict:
        call_count["n"] += 1
        response = {"echo": payload, "call": call_count["n"]}
        if idem is not None:
            await idem.record(status=200, body=response)
            if idem.replayed and idem.replayed_body is not None:
                # Race loser path: defer to the winning response.
                return json.loads(idem.replayed_body)
        return response

    app.state.call_count = call_count
    try:
        yield app
    finally:
        reset_idempotency_store()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestEndToEnd:
    def test_first_call_with_fresh_key_persists_and_returns(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        resp = client.post(
            "/send",
            headers={"Idempotency-Key": "key-fresh"},
            json={"channel_id": "+1", "text": "hi"},
        )
        assert resp.status_code == 200
        assert REPLAY_HEADER not in resp.headers
        assert resp.json()["echo"] == {"channel_id": "+1", "text": "hi"}

        # Row is in the store. Read via plain sqlite3 to avoid juggling event
        # loops between TestClient and the async engine.
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT key, response_status FROM idempotency_keys WHERE key = ?",
                ("key-fresh",),
            ).fetchone()
        assert row == ("key-fresh", 200)

    def test_second_call_same_key_same_body_returns_cached_with_replay_header(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        body = {"channel_id": "+1", "text": "hi"}
        first = client.post(
            "/send",
            headers={"Idempotency-Key": "key-replay"},
            json=body,
        )
        assert first.status_code == 200
        assert app.state.call_count["n"] == 1
        first_body = first.json()

        second = client.post(
            "/send",
            headers={"Idempotency-Key": "key-replay"},
            json=body,
        )
        assert second.status_code == 200
        assert second.headers[REPLAY_HEADER] == "true"
        assert second.json() == first_body
        # Handler did NOT run again — replay short-circuited.
        assert app.state.call_count["n"] == 1

    def test_second_call_same_key_different_body_returns_422(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        client.post(
            "/send",
            headers={"Idempotency-Key": "key-diff"},
            json={"channel_id": "+1", "text": "first"},
        )
        assert app.state.call_count["n"] == 1

        bad = client.post(
            "/send",
            headers={"Idempotency-Key": "key-diff"},
            json={"channel_id": "+1", "text": "second"},
        )
        assert bad.status_code == 422
        body = bad.json()
        assert body["error"]["code"] == "IDEMPOTENCY_KEY_REUSE"
        # Handler did NOT run for the colliding request.
        assert app.state.call_count["n"] == 1

    def test_key_order_in_body_does_not_trigger_422(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """Same logical body with reordered JSON keys is treated as a replay."""

        # First call sends keys in order A, B.
        first = client.post(
            "/send",
            headers={
                "Idempotency-Key": "key-order",
                "Content-Type": "application/json",
            },
            content=b'{"a":1,"b":2}',
        )
        assert first.status_code == 200
        # Second call sends the same logical body with keys B, A.
        second = client.post(
            "/send",
            headers={
                "Idempotency-Key": "key-order",
                "Content-Type": "application/json",
            },
            content=b'{"b":2,"a":1}',
        )
        assert second.status_code == 200
        assert second.headers[REPLAY_HEADER] == "true"
        assert app.state.call_count["n"] == 1

    def test_missing_idempotency_key_runs_handler_every_time(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        body = {"channel_id": "+1", "text": "no-key"}
        for _ in range(3):
            resp = client.post("/send", json=body)
            assert resp.status_code == 200
            assert REPLAY_HEADER not in resp.headers
        assert app.state.call_count["n"] == 3

    def test_blank_idempotency_key_treated_as_missing(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        resp = client.post(
            "/send",
            headers={"Idempotency-Key": "   "},
            json={"channel_id": "+1", "text": "x"},
        )
        assert resp.status_code == 200
        assert REPLAY_HEADER not in resp.headers
        # Second call with a blank header also runs the handler again.
        resp = client.post(
            "/send",
            headers={"Idempotency-Key": ""},
            json={"channel_id": "+1", "text": "x"},
        )
        assert resp.status_code == 200
        assert app.state.call_count["n"] == 2

    def test_expired_cache_entry_lets_handler_run_again(
        self,
        client: TestClient,
        clock: _FakeClock,
        app: FastAPI,
    ) -> None:
        body = {"channel_id": "+1", "text": "hi"}
        first = client.post(
            "/send",
            headers={"Idempotency-Key": "key-exp"},
            json=body,
        )
        assert first.status_code == 200
        assert app.state.call_count["n"] == 1

        # Push past the 24 h TTL — the next call must NOT short-circuit.
        clock.advance(DEFAULT_TTL + timedelta(seconds=1))

        second = client.post(
            "/send",
            headers={"Idempotency-Key": "key-exp"},
            json=body,
        )
        assert second.status_code == 200
        assert REPLAY_HEADER not in second.headers
        assert app.state.call_count["n"] == 2
