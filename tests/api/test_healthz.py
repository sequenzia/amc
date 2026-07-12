"""Tests for ``GET /healthz`` (spec §7.4.10).

Coverage walks the response shape and the connector-state mapping:

* Functional
    - 200 OK with the spec §7.4.10 envelope shape.
    - ``status`` is the literal string ``"ok"``.
    - ``version`` mirrors ``project.version`` from ``pyproject.toml``.
    - ``uptime_seconds`` is a non-negative integer.
    - ``last_message_at`` is the most-recent ``messages.created_at`` per source.
    - ``webhook_queue`` aggregates ``pending`` and ``dead`` correctly; the
      ``delivered`` bucket is omitted from the response.
* Connector states
    - No connector registered → ``"disabled"``.
    - Connector registered with ``degraded=False`` → ``"ok"``.
    - Connector registered with ``degraded=True`` → ``"degraded"``.
* Auth
    - Missing bearer → 401 ``UNAUTHORIZED`` (spec §7.4.12 envelope).
    - Wrong bearer → 401 ``UNAUTHORIZED``.
    - No ``X-Agent-ID`` is required (operator endpoint).

The suite spins up a fresh in-test FastAPI app per case, applies
``alembic upgrade head`` to a tmp_path SQLite file, seeds rows directly via
``sqlite3``, and registers fakes in the connector registry.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amg.api.healthz import (
    KNOWN_SOURCES,
    get_cached_version,
    load_version,
    record_start_time,
    register_connector,
    reset_cached_version,
    reset_connector_registry,
    reset_start_time,
    router,
)
from amg.core.auth import configure_bearer_token, reset_bearer_token
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.errors import register_exception_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
PYPROJECT = REPO_ROOT / "pyproject.toml"

BEARER = "test-bearer-token"
AUTH_HEADERS = {"Authorization": f"Bearer {BEARER}"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeConnector:
    """Stand-in for a connector — exposes ``degraded`` per ``ConnectorLike``.

    Mutable so a test can flip ``degraded`` after registration without
    re-wiring the app.
    """

    degraded: bool = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    path = tmp_path / "healthz.db"
    monkeypatch.setenv("AMG_DB_PATH", str(path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    return path


@pytest.fixture(autouse=True)
def reset_globals() -> Iterator[None]:
    """Reset module-level caches between tests for isolation."""
    reset_bearer_token()
    reset_connector_registry()
    reset_start_time()
    reset_cached_version()
    yield
    reset_bearer_token()
    reset_connector_registry()
    reset_start_time()
    reset_cached_version()


@pytest.fixture
def app(db_path: Path) -> Iterator[FastAPI]:
    """Build an in-test FastAPI app wired to the migrated tmp_path DB."""
    configure_bearer_token(BEARER)
    load_version(PYPROJECT)
    record_start_time()  # capture monotonic now

    application = FastAPI()
    register_exception_handlers(application)

    engine = create_engine_from_env(db_path_override=db_path)
    factory = create_session_factory(engine)
    application.state.session_factory = factory
    # Mirror the production wiring sequence — the route prefers
    # request.app.state.start_time when present.
    application.state.start_time = 0.0  # measured in monotonic seconds

    application.include_router(router)

    try:
        yield application
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Seeding helpers — synchronous sqlite3 so we can populate before the app boots
# ---------------------------------------------------------------------------


def _seed_baseline(db_path: Path) -> None:
    """Insert one channel + sender per source so messages can FK-reference them."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, 'dm')",
            ("imessage", "+15551234567"),
        )
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, 'dm')",
            ("discord", "discord:dm:1234567890"),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES (?, ?, ?, 'allowed')",
            ("imessage", "+15551234567", "Alice"),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, allowlist_status) "
            "VALUES (?, ?, ?, 'allowed')",
            ("discord", "discord_user_42", "Bob"),
        )
        conn.commit()
    finally:
        conn.close()


def _msg_id(suffix: str) -> str:
    """Return a valid ``msg_<26-char Crockford base32>`` id."""
    base = suffix.upper().ljust(26, "A")[:26]
    allowed = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    cleaned = "".join(c if c in allowed else "A" for c in base)
    return f"msg_{cleaned}"


def _insert_message(
    db_path: Path,
    *,
    msg_id: str,
    source: str,
    channel_id: str,
    sender_id: str,
    message_ts: str,
    created_at: str,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts, created_at) "
            "VALUES (?, ?, ?, 'dm', ?, ?, 'inbound', 'allowed', ?, ?)",
            (msg_id, source, channel_id, sender_id, "hi", message_ts, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_webhook_delivery(
    db_path: Path,
    *,
    delivery_id: str,
    message_id: str,
    status: str,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO webhook_deliveries "
            "(id, message_id, attempt, status) VALUES (?, ?, 0, ?)",
            (delivery_id, message_id, status),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Functional: response shape
# ---------------------------------------------------------------------------


class TestResponseShape:
    def test_returns_200_with_spec_envelope(self, client: TestClient) -> None:
        resp = client.get("/healthz", headers=AUTH_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Top-level keys per spec §7.4.10.
        assert set(body.keys()) == {
            "status",
            "uptime_seconds",
            "connectors",
            "webhook_queue",
            "version",
        }
        assert body["status"] == "ok"
        assert isinstance(body["uptime_seconds"], int)
        assert body["uptime_seconds"] >= 0
        # Both connectors present, even when nothing is registered.
        assert set(body["connectors"].keys()) == set(KNOWN_SOURCES)
        for source in KNOWN_SOURCES:
            entry = body["connectors"][source]
            assert set(entry.keys()) == {"state", "last_message_at"}
        # webhook_queue exposes only pending + dead.
        assert set(body["webhook_queue"].keys()) == {"pending", "dead"}
        assert body["webhook_queue"]["pending"] == 0
        assert body["webhook_queue"]["dead"] == 0

    def test_version_matches_pyproject(self, client: TestClient) -> None:
        resp = client.get("/healthz", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["version"] == get_cached_version()
        # Sanity check — the cached version is what's actually in pyproject.
        assert resp.json()["version"] == "0.1.0"

    def test_no_connector_registered_reports_disabled(self, client: TestClient) -> None:
        resp = client.get("/healthz", headers=AUTH_HEADERS)
        body = resp.json()
        assert body["connectors"]["imessage"]["state"] == "disabled"
        assert body["connectors"]["discord"]["state"] == "disabled"

    def test_no_messages_yet_reports_null_last_message_at(self, client: TestClient) -> None:
        resp = client.get("/healthz", headers=AUTH_HEADERS)
        body = resp.json()
        assert body["connectors"]["imessage"]["last_message_at"] is None
        assert body["connectors"]["discord"]["last_message_at"] is None


# ---------------------------------------------------------------------------
# Functional: connector states
# ---------------------------------------------------------------------------


class TestConnectorStates:
    def test_registered_healthy_connector_reports_ok(self, client: TestClient) -> None:
        register_connector("discord", FakeConnector(degraded=False))
        resp = client.get("/healthz", headers=AUTH_HEADERS)
        body = resp.json()
        assert body["connectors"]["discord"]["state"] == "ok"
        # Other source still disabled.
        assert body["connectors"]["imessage"]["state"] == "disabled"

    def test_registered_degraded_connector_reports_degraded(
        self, client: TestClient
    ) -> None:
        register_connector("imessage", FakeConnector(degraded=True))
        resp = client.get("/healthz", headers=AUTH_HEADERS)
        body = resp.json()
        assert body["connectors"]["imessage"]["state"] == "degraded"

    def test_both_connectors_registered_independently(self, client: TestClient) -> None:
        register_connector("discord", FakeConnector(degraded=False))
        register_connector("imessage", FakeConnector(degraded=True))
        resp = client.get("/healthz", headers=AUTH_HEADERS)
        body = resp.json()
        assert body["connectors"]["discord"]["state"] == "ok"
        assert body["connectors"]["imessage"]["state"] == "degraded"

    def test_degraded_flag_is_read_dynamically(self, client: TestClient) -> None:
        """Flipping ``connector.degraded`` after registration is reflected on next call."""
        connector = FakeConnector(degraded=False)
        register_connector("discord", connector)

        before = client.get("/healthz", headers=AUTH_HEADERS).json()
        assert before["connectors"]["discord"]["state"] == "ok"

        connector.degraded = True
        after = client.get("/healthz", headers=AUTH_HEADERS).json()
        assert after["connectors"]["discord"]["state"] == "degraded"

    def test_register_unknown_source_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown source"):
            register_connector("twitter", FakeConnector())


# ---------------------------------------------------------------------------
# Functional: last_message_at + webhook_queue
# ---------------------------------------------------------------------------


class TestLastMessageAt:
    def test_returns_max_created_at_per_source(
        self, db_path: Path, client: TestClient
    ) -> None:
        _seed_baseline(db_path)
        # Two iMessage rows; the second is more recent.
        _insert_message(
            db_path,
            msg_id=_msg_id("01"),
            source="imessage",
            channel_id="+15551234567",
            sender_id="+15551234567",
            message_ts="2026-04-25T15:32:11Z",
            created_at="2026-04-25T15:32:11.000Z",
        )
        _insert_message(
            db_path,
            msg_id=_msg_id("02"),
            source="imessage",
            channel_id="+15551234567",
            sender_id="+15551234567",
            message_ts="2026-04-25T16:00:00Z",
            created_at="2026-04-25T16:00:00.500Z",
        )
        # One Discord row.
        _insert_message(
            db_path,
            msg_id=_msg_id("03"),
            source="discord",
            channel_id="discord:dm:1234567890",
            sender_id="discord_user_42",
            message_ts="2026-04-25T17:00:00Z",
            created_at="2026-04-25T17:00:00.250Z",
        )

        resp = client.get("/healthz", headers=AUTH_HEADERS)
        body = resp.json()
        assert body["connectors"]["imessage"]["last_message_at"] == "2026-04-25T16:00:00.500Z"
        assert body["connectors"]["discord"]["last_message_at"] == "2026-04-25T17:00:00.250Z"


class TestWebhookQueueCounts:
    def test_groups_pending_and_dead(self, db_path: Path, client: TestClient) -> None:
        _seed_baseline(db_path)
        msg_id = _msg_id("WH")
        _insert_message(
            db_path,
            msg_id=msg_id,
            source="discord",
            channel_id="discord:dm:1234567890",
            sender_id="discord_user_42",
            message_ts="2026-04-25T17:00:00Z",
            created_at="2026-04-25T17:00:00.000Z",
        )
        _insert_webhook_delivery(db_path, delivery_id="d1", message_id=msg_id, status="pending")
        _insert_webhook_delivery(db_path, delivery_id="d2", message_id=msg_id, status="pending")
        _insert_webhook_delivery(db_path, delivery_id="d3", message_id=msg_id, status="dead")
        # delivered rows should NOT show up in the response.
        _insert_webhook_delivery(db_path, delivery_id="d4", message_id=msg_id, status="delivered")
        _insert_webhook_delivery(db_path, delivery_id="d5", message_id=msg_id, status="delivered")

        resp = client.get("/healthz", headers=AUTH_HEADERS)
        body = resp.json()
        assert body["webhook_queue"] == {"pending": 2, "dead": 1}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    # Bearer auth raises ``HTTPException(detail=...)`` which FastAPI nests under
    # ``body["detail"]``. Task #27/#35 will introduce a global exception handler
    # that flattens this to the canonical envelope (spec §7.4.12). Until then
    # the tests assert against the nested shape — same as the other API tests
    # in this wave.
    def test_missing_bearer_returns_401(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 401
        body = resp.json()
        assert body["detail"]["error"]["code"] == "UNAUTHORIZED"

    def test_wrong_bearer_returns_401(self, client: TestClient) -> None:
        resp = client.get("/healthz", headers={"Authorization": "Bearer not-the-token"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"]["code"] == "UNAUTHORIZED"

    def test_does_not_require_x_agent_id(self, client: TestClient) -> None:
        """``/healthz`` is operator-facing — no X-Agent-ID needed."""
        resp = client.get("/healthz", headers=AUTH_HEADERS)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# uptime_seconds
# ---------------------------------------------------------------------------


class TestUptimeSeconds:
    def test_uptime_uses_app_state_start_time_when_present(
        self, app: FastAPI, db_path: Path  # noqa: ARG002
    ) -> None:
        """Setting ``app.state.start_time`` to a value far in the past yields a positive uptime."""
        import time as time_module

        # Set start_time to a value 100 seconds in the past (in monotonic terms).
        app.state.start_time = time_module.monotonic() - 100.0

        with TestClient(app) as c:
            resp = c.get("/healthz", headers=AUTH_HEADERS)

        assert resp.status_code == 200
        # Allow a generous lower bound — uptime should be at least 100s.
        assert resp.json()["uptime_seconds"] >= 100

    def test_uptime_falls_back_to_module_cache(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        """When ``app.state.start_time`` is unset, the module-level cache is used."""
        import time as time_module

        configure_bearer_token(BEARER)
        load_version(PYPROJECT)
        # Record a start time 50s in the past.
        record_start_time(time_module.monotonic() - 50.0)

        application = FastAPI()
        register_exception_handlers(application)
        engine = create_engine_from_env(db_path_override=db_path)
        factory = create_session_factory(engine)
        application.state.session_factory = factory
        # Note: NOT setting application.state.start_time here.
        application.include_router(router)

        try:
            with TestClient(application) as c:
                resp = c.get("/healthz", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            assert resp.json()["uptime_seconds"] >= 50
        finally:
            with contextlib.suppress(RuntimeError):
                asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# load_version
# ---------------------------------------------------------------------------


class TestLoadVersion:
    def test_load_version_caches_value(self, tmp_path: Path) -> None:
        fixture = tmp_path / "pyproject.toml"
        fixture.write_text('[project]\nname = "thing"\nversion = "9.9.9"\n')
        result = load_version(fixture)
        assert result == "9.9.9"
        assert get_cached_version() == "9.9.9"

    def test_load_version_missing_project_section_raises(self, tmp_path: Path) -> None:
        fixture = tmp_path / "pyproject.toml"
        fixture.write_text("[other]\nfoo = 1\n")
        with pytest.raises(RuntimeError, match="missing or non-table"):
            load_version(fixture)

    def test_load_version_missing_version_field_raises(self, tmp_path: Path) -> None:
        fixture = tmp_path / "pyproject.toml"
        fixture.write_text('[project]\nname = "thing"\n')
        with pytest.raises(RuntimeError, match="non-empty string"):
            load_version(fixture)

    def test_load_version_empty_version_raises(self, tmp_path: Path) -> None:
        fixture = tmp_path / "pyproject.toml"
        fixture.write_text('[project]\nname = "thing"\nversion = ""\n')
        with pytest.raises(RuntimeError, match="non-empty string"):
            load_version(fixture)
