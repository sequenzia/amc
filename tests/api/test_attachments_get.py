"""Tests for ``GET /attachments/{id}`` (spec §7.4.9).

Coverage map
------------
Functional:
* Hit on a re-hosted attachment → 200 with bytes, ``Content-Type`` from
  the row, ``Content-Length`` matching ``size_bytes``,
  ``Content-Disposition: inline``.
* Unknown id → 404 ``MESSAGE_NOT_FOUND``.
* ``bytes_path IS NULL`` (retention sweeper deleted bytes) → 404.
* Row points to a missing file on disk → 404 + ERROR-level log.

Edge cases:
* Path-param regex on ``AttachmentId`` rejects non-``att_``-prefixed
  slugs and short / mistyped ids.

Error handling:
* Missing bearer header → 401 ``UNAUTHORIZED``.
* Wrong bearer token → 401 ``UNAUTHORIZED``.
* ``X-Agent-ID`` is NOT required for this endpoint (spec §7.4 header
  table) — request without it should still succeed.

Test infrastructure
-------------------
* ``tmp_path`` SQLite + ``alembic upgrade head`` (mirrors the sibling
  ``tests/api/test_messages_get.py`` pattern).
* In-test FastAPI app fixture mounting only the attachments_get router
  so we are not coupled to the wave-mate routers' wiring.
* ``configure_session_factory`` for the DB and ``configure_bearer_token``
  for auth.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from amg.api.attachments_get import (
    configure_session_factory,
    reset_session_factory,
    router,
)
from amg.core.auth import configure_bearer_token, reset_bearer_token
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.errors import register_exception_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER_TOKEN = "test-bearer-token-attachments"

# Valid Crockford-base32 ULIDs (26 chars; alphabet excludes I, L, O, U).
ATT_ID_PRESENT = "att_01HQXR9F3TQX3MT5N8K0JVB7G2"
ATT_ID_DELETED = "att_01HQXR9G7VWX5NF8P2T0HJBC4Y"
ATT_ID_BROKEN = "att_01HQXR9HBYZ7QHKBR4VC5KMD8N"
ATT_ID_MISSING = "att_01HQXR9JDA28SJMCS6XE7NPF9P"

MSG_ID = "msg_01HQXR9KEC39TKNCT7YF8PQG0Q"
CHANNEL_ID = "+15551234567"
SENDER_ID = "+15550001111"

# Tiny PNG (1x1 transparent) — opaque payload that exercises a binary mime.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae4260"
    "82"
)


# ---------------------------------------------------------------------------
# Log capture fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def structlog_to_stdlib() -> Iterator[None]:
    """Bridge structlog into stdlib so ``caplog`` captures structured records.

    Mirrors the pattern in ``tests/core/test_attachments.py``: the route uses
    ``structlog.get_logger(...)`` directly; without this configuration the
    output goes to stdout via the default JSONRenderer chain and ``caplog``
    sees nothing.
    """
    structlog.configure(
        processors=[structlog.stdlib.add_log_level, structlog.stdlib.render_to_log_kwargs],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    try:
        yield
    finally:
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    path = tmp_path / "attachments_get.db"
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
def store_dir(tmp_path: Path) -> Path:
    """Directory under which fixture attachment bytes are written."""
    d = tmp_path / "attachments_store"
    d.mkdir()
    return d


@pytest.fixture
def seeded_db(db_path: Path, store_dir: Path) -> Path:
    """Seed channel, sender, message, and four attachment rows.

    Rows:

    1. ``ATT_ID_PRESENT`` — ``bytes_path`` set, file present on disk.
    2. ``ATT_ID_DELETED`` — ``bytes_path IS NULL`` (sweeper removed).
    3. ``ATT_ID_BROKEN`` — ``bytes_path`` set but file missing on disk.
    4. ``ATT_ID_MISSING`` — not inserted; the route should 404.
    """
    # Write the present-attachment file so the route can stream it.
    present_path = store_dir / ATT_ID_PRESENT
    present_path.write_bytes(PNG_BYTES)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, ?)",
            ("imessage", CHANNEL_ID, "dm"),
        )
        conn.execute(
            "INSERT INTO senders (source, sender_id, display_name, "
            "                     allowlist_status, person_id, "
            "                     first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "imessage",
                SENDER_ID,
                "Allowed Person",
                "allowed",
                None,
                "2026-05-03T12:00:00.000Z",
                "2026-05-03T12:00:00.000Z",
            ),
        )
        conn.execute(
            "INSERT INTO messages (id, source, channel_id, channel_type, "
            "                      sender_id, text, direction, "
            "                      allowlist_status, message_ts, "
            "                      attachments_json, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                MSG_ID,
                "imessage",
                CHANNEL_ID,
                "dm",
                SENDER_ID,
                "with attachment",
                "inbound",
                "allowed",
                "2026-05-03T12:30:00.000Z",
                None,
                None,
            ),
        )

        # 1. Re-hosted, bytes present.
        conn.execute(
            "INSERT INTO attachments (id, message_id, mime, size_bytes, "
            "                         bytes_path, original_url_or_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ATT_ID_PRESENT,
                MSG_ID,
                "image/png",
                len(PNG_BYTES),
                str(present_path),
                "/tmp/source-tiny.png",
            ),
        )

        # 2. Retention sweeper deleted bytes — bytes_path NULL.
        conn.execute(
            "INSERT INTO attachments (id, message_id, mime, size_bytes, "
            "                         bytes_path, original_url_or_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ATT_ID_DELETED,
                MSG_ID,
                "image/png",
                len(PNG_BYTES),
                None,
                "/tmp/deleted-source.png",
            ),
        )

        # 3. Row claims bytes are at a path; file does not exist on disk.
        broken_path = store_dir / "this-file-does-not-exist"
        conn.execute(
            "INSERT INTO attachments (id, message_id, mime, size_bytes, "
            "                         bytes_path, original_url_or_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ATT_ID_BROKEN,
                MSG_ID,
                "image/png",
                len(PNG_BYTES),
                str(broken_path),
                "/tmp/broken-source.png",
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
    """Mount a minimal FastAPI app exposing the attachments_get router."""
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
    return {"Authorization": f"Bearer {BEARER_TOKEN}"}


# ---------------------------------------------------------------------------
# Functional tests
# ---------------------------------------------------------------------------


class TestPresent:
    def test_returns_bytes_with_headers(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.get(f"/attachments/{ATT_ID_PRESENT}", headers=auth_headers)
        assert resp.status_code == 200, resp.text

        # Body is the exact bytes we wrote to disk.
        assert resp.content == PNG_BYTES

        # Headers from the row.
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["content-length"] == str(len(PNG_BYTES))
        assert resp.headers["content-disposition"] == "inline"

    def test_succeeds_without_x_agent_id(
        self,
        client: TestClient,
    ) -> None:
        # §7.4 header table: this endpoint does NOT require X-Agent-ID.
        resp = client.get(
            f"/attachments/{ATT_ID_PRESENT}",
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == PNG_BYTES


class TestDeleted:
    def test_bytes_path_null_returns_404(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.get(f"/attachments/{ATT_ID_DELETED}", headers=auth_headers)
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "MESSAGE_NOT_FOUND"


class TestMissing:
    def test_unknown_id_returns_404(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = client.get(f"/attachments/{ATT_ID_MISSING}", headers=auth_headers)
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "MESSAGE_NOT_FOUND"


class TestBrokenFileOnDisk:
    def test_missing_file_returns_404_and_logs_error(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        caplog: pytest.LogCaptureFixture,
        structlog_to_stdlib: None,  # noqa: ARG002 — fixture installs the bridge
    ) -> None:
        # Capture ERROR-level records on the route's named logger.
        caplog.set_level(logging.ERROR, logger="amg.api.attachments_get")

        resp = client.get(f"/attachments/{ATT_ID_BROKEN}", headers=auth_headers)
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "MESSAGE_NOT_FOUND"

        # The route MUST log an ERROR identifying the broken row so the
        # operator can investigate. structlog (bridged into stdlib by the
        # fixture) renders the structlog ``event`` as the stdlib record
        # message and threads kwargs through as record attributes.
        error_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR
            and r.name == "amg.api.attachments_get"
            and r.getMessage() == "attachment_bytes_missing_on_disk"
        ]
        assert error_records, (
            "Expected an ERROR-level 'attachment_bytes_missing_on_disk' log; "
            f"saw: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        record = error_records[0]
        assert getattr(record, "attachment_id", None) == ATT_ID_BROKEN
        assert "this-file-does-not-exist" in getattr(record, "bytes_path", "")


# ---------------------------------------------------------------------------
# Error handling: auth
# ---------------------------------------------------------------------------


def _error_code(body: dict) -> str:
    """Extract canonical error code from either envelope shape.

    The route-side ``AMGError`` handler emits ``{"error": {"code": ...}}``
    directly. The auth dependencies (``amg.core.auth``) still raise stock
    ``HTTPException(detail=...)`` which FastAPI nests under ``"detail"``;
    task #27/#35 will flatten it. Tolerate both shapes.
    """
    if "error" in body:
        return body["error"]["code"]
    if "detail" in body and isinstance(body["detail"], dict):
        return body["detail"]["error"]["code"]
    raise AssertionError(f"Unexpected error body shape: {body!r}")


class TestAuth:
    def test_missing_bearer_returns_401(self, client: TestClient) -> None:
        resp = client.get(f"/attachments/{ATT_ID_PRESENT}")
        assert resp.status_code == 401
        assert _error_code(resp.json()) == "UNAUTHORIZED"

    def test_invalid_bearer_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            f"/attachments/{ATT_ID_PRESENT}",
            headers={"Authorization": "Bearer not-the-real-token"},
        )
        assert resp.status_code == 401
        assert _error_code(resp.json()) == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Routing-collision regression
# ---------------------------------------------------------------------------


class TestPathParamRegex:
    """The ``AttachmentId`` regex on ``{attachment_id}`` rejects non-ULID slugs.

    This guarantees future sibling routes under ``/attachments/...`` (none
    today, but the tags are scoped) won't be shadowed by the ``{id}`` route.
    """

    @pytest.mark.parametrize(
        "slug",
        [
            "list",
            "missing",
            "att_too_short",
            "wrong_prefix_01HQXR9F3TQX3MT5N8K0JVB7G2",
            # ULID body uses Crockford base32; "I" is excluded.
            "att_01HQXR9F3TQX3MT5N8K0JVB7GI",
        ],
    )
    def test_non_ulid_slug_does_not_match_route(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        slug: str,
    ) -> None:
        resp = client.get(f"/attachments/{slug}", headers=auth_headers)
        # Either 404 (no route matched) or 400 (path validation failed).
        # The critical point: NEVER 200 with bytes.
        assert resp.status_code != 200
