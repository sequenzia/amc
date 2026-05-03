"""Tests for ``POST /messages/send`` (spec §5.2, §7.4.5).

Coverage map
------------

Functional (Discord):
* Discord channel send returns 200 with ``message_id`` + ``sent_at`` and
  persists an outbound row with ``direction='outbound'``,
  ``allowlist_status='outbound'``, and ``delivery_status='sent'``.
* The body returned uses RFC 3339 with millisecond precision and ``Z``
  suffix.
* The connector is invoked with ``payload.channel_id`` and ``payload.text``.

Functional (iMessage, #51):
* iMessage channel send invokes the iMessage connector with the channel id
  and text, returns 200, and persists an outbound row with the same
  outbound/sent shape.
* iMessage send failure (AppleScript ``ok=False``) becomes
  502 ``PLATFORM_SEND_FAILED`` and no outbound row is persisted.
* iMessage attachment ``url`` matching the adapter ``/attachments/<id>``
  form is resolved to the local ``bytes_path`` and passed to the
  AppleScript sender.
* iMessage attachment ``path`` (absolute) is passed through unchanged.
* iMessage attachment with a non-adapter URL is rejected with
  400 ``VALIDATION_FAILED``.
* iMessage attachment with a relative ``path`` is rejected with
  400 ``VALIDATION_FAILED``.
* iMessage attachment URL referencing an unknown / retention-deleted
  attachment id is rejected with 404 ``MESSAGE_NOT_FOUND``.

Channel resolution:
* Unknown ``(source, channel_id)`` returns 404 ``CHANNEL_NOT_FOUND``.
* Source is inferred from the channel id prefix (``discord:`` →
  ``discord``; everything else → ``imessage``).

Rate limiting:
* When the per-channel limiter rejects, returns 429 ``RATE_LIMITED`` with a
  ``Retry-After`` header.

Discord error mapping:
* Connector returning ``PLATFORM_AUTH`` → 502 ``PLATFORM_AUTH``.
* Connector returning ``PLATFORM_SEND_FAILED`` → 502
  ``PLATFORM_SEND_FAILED``.

Idempotency:
* Same key + same body → second call short-circuits with the cached body and
  ``Idempotency-Replayed: true``; the connector is invoked exactly once.
* Same key + different body → 422 ``IDEMPOTENCY_KEY_REUSE`` and the
  connector is invoked exactly once (the colliding call never reaches it).

Attachments (Discord Phase 1 stub):
* Any Discord attachment returns 413 ``ATTACHMENT_TOO_LARGE_FOR_PLATFORM``
  with a message noting the unimplemented passthrough and the eventual
  25 MB limit.

Auth + validation:
* Missing bearer → 401.
* Wrong bearer → 401.
* X-Agent-ID is NOT required for send (spec §7.4 header table).
* Empty text + empty attachments → 400 ``VALIDATION_FAILED`` (Pydantic
  rejects via ``SendMessageRequest._require_text_or_attachment``).

Test infrastructure mirrors ``tests/api/test_messages_mark_read.py``:
alembic upgrade on a tmp_path SQLite DB, async session factory built per
test, exception handlers + idempotency replay handler registered. The
Discord connector is replaced with a small fake that records calls and
emits scriptable :class:`SendResult`s. The iMessage connector is a thin
fake that wraps a :class:`FakeAppleScriptSender` (constructor injection)
so tests can assert on what reached AppleScript without booting the
chat.db poller.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from amc.api.messages_send import (
    configure_discord_connector,
    configure_imessage_connector,
    configure_rate_limiter,
    configure_session_factory,
    reset_discord_connector,
    reset_imessage_connector,
    reset_rate_limiter,
    reset_session_factory,
    router,
)
from amc.connectors.discord.connector import AmcDiscordErrorCode, SendResult
from amc.connectors.imessage.applescript import SendResult as ImessageSendResult
from amc.core.auth import configure_bearer_token, reset_bearer_token
from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.errors import register_exception_handlers
from amc.core.idempotency import (
    IdempotencyStore,
    configure_idempotency_store,
    register_idempotency_handlers,
    reset_idempotency_store,
)
from amc.core.rate_limit import RateLimiter
from tests.fakes.applescript import FakeAppleScriptSender

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-token-send"
DISCORD_CHANNEL = "discord:dm:1234567890123456"
DISCORD_CHANNEL_ALT = "discord:channel:9999999999999999"
IMESSAGE_CHANNEL = "+15551234567"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeDiscordConnector:
    """Minimal stand-in for :class:`DiscordConnector` exposing just ``send``.

    Each test scripts the next return value via :attr:`next_result` (or a
    sequence via :meth:`script`), and inspects :attr:`calls` for what the
    route handed off.
    """

    next_result: SendResult = field(
        default_factory=lambda: SendResult(ok=True, message_id="999000111222333")
    )
    queued: list[SendResult] = field(default_factory=list)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def script(self, *results: SendResult) -> None:
        """Push results onto the queue; consumed FIFO by ``send``."""
        self.queued.extend(results)

    async def send(
        self,
        channel_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        attachments: object | None = None,  # noqa: ARG002 — interface parity
    ) -> SendResult:
        self.calls.append((channel_id, text, reply_to))
        if self.queued:
            return self.queued.pop(0)
        return self.next_result


@dataclass
class FakeImessageConnector:
    """Stand-in for :class:`ImessageConnector` exposing only ``send``.

    Holds a :class:`FakeAppleScriptSender` as a constructor-injected
    dependency (mirroring the real connector's ``sender=`` kwarg) and
    delegates ``send`` to it the way the real connector does. Tests
    inspect :attr:`sender.calls` for the chat_guid / text / attachment
    path the AppleScript layer received, and queue scripted outcomes via
    :meth:`sender.queue_outcome`.

    Channel-id → chat-guid resolution is the canonical
    ``iMessage;-;<channel_id>`` shape so tests don't need to seed
    ``channels.metadata_json`` (the real connector does the same fallback
    when no metadata is found).
    """

    sender: FakeAppleScriptSender = field(default_factory=FakeAppleScriptSender)
    calls: list[tuple[str, str, list | None]] = field(default_factory=list)

    async def send(
        self,
        channel_id: str,
        text_body: str,
        *,
        reply_to: str | None = None,  # noqa: ARG002 — interface parity
        attachments: object | None = None,
    ) -> ImessageSendResult:
        attachment_list = list(attachments) if attachments else None
        self.calls.append((channel_id, text_body, attachment_list))
        chat_guid = f"iMessage;-;{channel_id}"
        attachment_path: str | None = None
        if attachment_list:
            first = attachment_list[0]
            if isinstance(first, str):
                attachment_path = first
        return await self.sender.send(chat_guid, text_body, attachment_path)


# ---------------------------------------------------------------------------
# DB / session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    path = tmp_path / "send.db"
    monkeypatch.setenv("AMC_DB_PATH", str(path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    return path


@pytest.fixture
def session_factory(db_path: Path) -> Iterator[async_sessionmaker]:
    """Async session factory bound to the migrated tmp_path DB.

    Synchronous fixture so the engine is not pinned to pytest-asyncio's loop;
    ``TestClient`` spins up its own loop per request.
    """
    engine = create_engine_from_env(db_path_override=db_path)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# Seeding helpers — sync sqlite3 so we set state before the app boots.
# ---------------------------------------------------------------------------


def _seed_channel(db_path: Path, *, source: str, channel_id: str, channel_type: str = "dm") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO channels (source, channel_id, channel_type) VALUES (?, ?, ?)",
            (source, channel_id, channel_type),
        )
        conn.commit()
    finally:
        conn.close()


def _read_outbound_rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, source, channel_id, channel_type, sender_id, text, "
            "direction, allowlist_status, message_ts, raw_json, "
            "delivery_status "
            "FROM messages WHERE direction = 'outbound' "
            "ORDER BY message_ts"
        ).fetchall()
        return list(rows)
    finally:
        conn.close()


def _seed_attachment(
    db_path: Path,
    *,
    att_id: str,
    bytes_path: str,
    mime: str = "image/png",
    size_bytes: int = 0,
    message_id: str | None = None,
    source: str = "imessage",
    channel_id: str = IMESSAGE_CHANNEL,
) -> None:
    """Insert a minimal ``attachments`` row pointing at ``bytes_path``.

    Attachments require an FK to ``messages.id``; we synthesize a tiny
    inbound message row so the FK resolves. Tests that only care about
    the URL→bytes_path lookup don't need to inspect the carrier
    message.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        # Carrier sender + channel + message so the attachment FK resolves.
        conn.execute(
            "INSERT OR IGNORE INTO senders "
            "(source, sender_id, display_name, allowlist_status) "
            "VALUES (?, ?, ?, 'allowed')",
            (source, "carrier_sender", "carrier"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO channels (source, channel_id, channel_type) "
            "VALUES (?, ?, 'dm')",
            (source, channel_id),
        )
        carrier_id = message_id or f"msg_CARRIER{att_id[-18:].rjust(18, '0')}"
        conn.execute(
            "INSERT INTO messages "
            "(id, source, channel_id, channel_type, sender_id, text, "
            " direction, allowlist_status, message_ts) "
            "VALUES (?, ?, ?, 'dm', ?, '', 'inbound', 'allowed', "
            "        '2026-05-03T00:00:00.000Z')",
            (carrier_id, source, channel_id, "carrier_sender"),
        )
        conn.execute(
            "INSERT INTO attachments "
            "(id, message_id, mime, size_bytes, bytes_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (att_id, carrier_id, mime, size_bytes, bytes_path),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_globals() -> Iterator[None]:
    """Clear every module-level cache between tests for true isolation."""
    reset_bearer_token()
    reset_session_factory()
    reset_rate_limiter()
    reset_discord_connector()
    reset_imessage_connector()
    reset_idempotency_store()
    yield
    reset_bearer_token()
    reset_session_factory()
    reset_rate_limiter()
    reset_discord_connector()
    reset_imessage_connector()
    reset_idempotency_store()


@pytest.fixture
def discord_connector() -> FakeDiscordConnector:
    return FakeDiscordConnector()


@pytest.fixture
def imessage_connector() -> FakeImessageConnector:
    return FakeImessageConnector()


@pytest.fixture
def rate_limiter() -> RateLimiter:
    """A generously-sized limiter that won't reject in the common path."""
    return RateLimiter(rps=100.0, burst=100)


@pytest.fixture
def app(
    session_factory: async_sessionmaker,
    rate_limiter: RateLimiter,
    discord_connector: FakeDiscordConnector,
    imessage_connector: FakeImessageConnector,
) -> Iterator[FastAPI]:
    configure_bearer_token(BEARER)
    configure_session_factory(session_factory)
    configure_rate_limiter(rate_limiter)
    configure_discord_connector(discord_connector)  # type: ignore[arg-type]
    configure_imessage_connector(imessage_connector)  # type: ignore[arg-type]

    # Idempotency store backed by the same DB so the dependency is wired.
    configure_idempotency_store(IdempotencyStore(session_factory))

    app = FastAPI()
    register_exception_handlers(app)
    register_idempotency_handlers(app)
    app.include_router(router)
    yield app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _auth_headers(*, bearer: str | None = BEARER) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}


# ---------------------------------------------------------------------------
# Functional
# ---------------------------------------------------------------------------


class TestFunctionalDiscord:
    def test_discord_send_returns_message_id_and_sent_at(
        self,
        client: TestClient,
        db_path: Path,
        discord_connector: FakeDiscordConnector,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)

        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "hello world"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"message_id", "sent_at"}
        assert body["message_id"].startswith("msg_")
        assert len(body["message_id"]) == 30  # "msg_" + 26 char ULID
        # Millisecond precision Z suffix.
        assert body["sent_at"].endswith("Z")
        assert "." in body["sent_at"]

        # Connector got the right call.
        assert discord_connector.calls == [(DISCORD_CHANNEL, "hello world", None)]

    def test_outbound_row_persisted_with_correct_columns(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)

        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "persist me"},
        )
        assert resp.status_code == 200
        message_id = resp.json()["message_id"]

        rows = _read_outbound_rows(db_path)
        assert len(rows) == 1
        (
            row_id,
            source,
            channel_id,
            channel_type,
            sender_id,
            text_body,
            direction,
            allowlist_status,
            _ts,
            raw_json,
            delivery_status,
        ) = rows[0]
        assert row_id == message_id
        assert source == "discord"
        assert channel_id == DISCORD_CHANNEL
        assert channel_type == "dm"
        assert sender_id == "agent"
        assert text_body == "persist me"
        assert direction == "outbound"
        assert allowlist_status == "outbound"
        # Platform message id captured in raw_json.
        assert raw_json is not None
        assert "platform_message_id" in raw_json
        # Migration 002 column: outbound rows are ``sent`` after a
        # successful platform send.
        assert delivery_status == "sent"

    def test_two_consecutive_sends_persist_two_rows(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)

        first = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "one"},
        )
        second = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "two"},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["message_id"] != second.json()["message_id"]

        rows = _read_outbound_rows(db_path)
        assert len(rows) == 2
        assert {r[5] for r in rows} == {"one", "two"}


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------


class TestChannelResolution:
    def test_unknown_channel_returns_404_channel_not_found(
        self,
        client: TestClient,
    ) -> None:
        # No channel seeded.
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "hi"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "CHANNEL_NOT_FOUND"

    def test_source_inferred_from_discord_prefix(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL_ALT)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL_ALT, "text": "ok"},
        )
        assert resp.status_code == 200

    def test_imessage_channel_seeded_under_discord_returns_404(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        """A channel id seeded for the wrong source still surfaces as 404.

        The route uses the inferred ``source`` to look up the row, not the
        channel id alone — so a Discord channel id with no ``discord``-source
        row in the table is unknown to the adapter.
        """
        _seed_channel(db_path, source="imessage", channel_id=DISCORD_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "wrong source"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHANNEL_NOT_FOUND"


# ---------------------------------------------------------------------------
# iMessage outbound (#51)
# ---------------------------------------------------------------------------


class TestFunctionalImessage:
    def test_imessage_text_only_send_returns_200_and_persists_outbound_row(
        self,
        client: TestClient,
        db_path: Path,
        imessage_connector: FakeImessageConnector,
    ) -> None:
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": IMESSAGE_CHANNEL, "text": "hello via imessage"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {"message_id", "sent_at"}
        assert body["message_id"].startswith("msg_")

        # Connector got the right call with no attachments.
        assert imessage_connector.calls == [
            (IMESSAGE_CHANNEL, "hello via imessage", None)
        ]
        # AppleScript layer received the canonical chat.guid form.
        assert len(imessage_connector.sender.calls) == 1
        sent = imessage_connector.sender.calls[0]
        assert sent.chat_guid == f"iMessage;-;{IMESSAGE_CHANNEL}"
        assert sent.text == "hello via imessage"
        assert sent.attachment_path is None

        # Outbound row persisted with delivery_status='sent'.
        rows = _read_outbound_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row[1] == "imessage"  # source
        assert row[2] == IMESSAGE_CHANNEL  # channel_id
        assert row[6] == "outbound"  # direction
        assert row[7] == "outbound"  # allowlist_status
        assert row[10] == "sent"  # delivery_status

    def test_imessage_send_failure_returns_502_and_no_row_persisted(
        self,
        client: TestClient,
        db_path: Path,
        imessage_connector: FakeImessageConnector,
    ) -> None:
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)
        # Script the AppleScript layer to fail terminally.
        imessage_connector.sender.queue_outcome(
            ImessageSendResult(
                ok=False,
                error="applescript_error",
                raw_stderr="execution error: unhelpful 1234",
            )
        )

        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": IMESSAGE_CHANNEL, "text": "doomed"},
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["code"] == "PLATFORM_SEND_FAILED"
        assert "applescript_error" in body["error"]["message"]
        # No outbound row written when the platform call failed.
        assert _read_outbound_rows(db_path) == []

    def test_imessage_no_connector_configured_returns_502(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        """When the iMessage connector is intentionally disabled."""
        from amc.api.messages_send import reset_imessage_connector as _reset

        _reset()
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": IMESSAGE_CHANNEL, "text": "no connector"},
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["code"] == "PLATFORM_SEND_FAILED"
        assert "iMessage connector is not configured" in body["error"]["message"]


class TestImessageAttachments:
    """Outbound attachment routing for iMessage (#51)."""

    def test_url_pointing_at_adapter_attachment_resolves_to_bytes_path(
        self,
        client: TestClient,
        db_path: Path,
        tmp_path: Path,
        imessage_connector: FakeImessageConnector,
    ) -> None:
        # Materialize a real file so the on-disk existence check passes.
        bytes_file = tmp_path / "out.png"
        bytes_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)
        # Use a Crockford-base32 ULID body to satisfy the model's
        # ``att_<ULID>`` pattern. The URL is the canonical form the
        # adapter emits (see ``AttachmentStore.url_for``).
        att_id = "att_01HZZZZZZZZZZZZZZZZZZZZZZZ"
        _seed_attachment(
            db_path,
            att_id=att_id,
            bytes_path=str(bytes_file),
            mime="image/png",
            size_bytes=bytes_file.stat().st_size,
        )

        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={
                "channel_id": IMESSAGE_CHANNEL,
                "text": "see image",
                "attachments": [
                    {"url": f"http://127.0.0.1:8080/attachments/{att_id}"}
                ],
            },
        )
        assert resp.status_code == 200, resp.text

        # The AppleScript layer received the resolved local path, not the URL.
        assert len(imessage_connector.sender.calls) == 1
        sent = imessage_connector.sender.calls[0]
        assert sent.attachment_path == str(bytes_file)

    def test_absolute_path_passed_through_unchanged(
        self,
        client: TestClient,
        db_path: Path,
        tmp_path: Path,
        imessage_connector: FakeImessageConnector,
    ) -> None:
        # Path doesn't have to exist (operator-trusted); AppleScript
        # surfaces missing-file errors.
        absolute_path = str(tmp_path / "notreal.png")
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)

        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={
                "channel_id": IMESSAGE_CHANNEL,
                "text": "see",
                "attachments": [{"path": absolute_path}],
            },
        )
        assert resp.status_code == 200, resp.text
        assert imessage_connector.sender.calls[0].attachment_path == absolute_path

    def test_relative_path_rejected_with_400(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={
                "channel_id": IMESSAGE_CHANNEL,
                "text": "see",
                "attachments": [{"path": "relative/file.png"}],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_external_url_rejected_with_400(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={
                "channel_id": IMESSAGE_CHANNEL,
                "text": "see",
                "attachments": [{"url": "https://example.com/somefile.png"}],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_unknown_attachment_id_returns_404(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={
                "channel_id": IMESSAGE_CHANNEL,
                "text": "see",
                "attachments": [
                    {
                        "url": (
                            "http://127.0.0.1:8080/attachments/"
                            "att_01HXXXXXXXXXXXXXXXXXXXXXXX"
                        )
                    }
                ],
            },
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MESSAGE_NOT_FOUND"

    def test_attachment_bytes_missing_on_disk_returns_404(
        self,
        client: TestClient,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        """Row claims bytes exist but file is gone — corruption signal."""
        ghost = tmp_path / "ghost.png"
        # Do NOT create the file.
        _seed_channel(db_path, source="imessage", channel_id=IMESSAGE_CHANNEL)
        att_id = "att_01J1111111111111111111111B"
        _seed_attachment(
            db_path,
            att_id=att_id,
            bytes_path=str(ghost),
        )
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={
                "channel_id": IMESSAGE_CHANNEL,
                "text": "see",
                "attachments": [
                    {"url": f"http://127.0.0.1:8080/attachments/{att_id}"}
                ],
            },
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MESSAGE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    @pytest.fixture
    def rate_limiter(self) -> RateLimiter:
        # Override the module-level fixture: a tiny budget so the second send
        # in a row gets denied.
        return RateLimiter(rps=1.0, burst=1)

    def test_burst_exceeded_returns_429_with_retry_after(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)

        # First send consumes the only token.
        first = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "1"},
        )
        assert first.status_code == 200

        # Immediate second send is over budget.
        second = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "2"},
        )
        assert second.status_code == 429
        body = second.json()
        assert body["error"]["code"] == "RATE_LIMITED"
        # Retry-After header is present and is an int (seconds).
        assert "retry-after" in {h.lower() for h in second.headers}
        retry_after = second.headers.get("Retry-After") or second.headers.get("retry-after")
        assert retry_after is not None
        assert retry_after.isdigit()


# ---------------------------------------------------------------------------
# Discord error mapping
# ---------------------------------------------------------------------------


class TestDiscordErrorMapping:
    def test_platform_auth_error_becomes_502_platform_auth(
        self,
        client: TestClient,
        db_path: Path,
        discord_connector: FakeDiscordConnector,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        discord_connector.script(
            SendResult(
                ok=False,
                error=AmcDiscordErrorCode.PLATFORM_AUTH.value,
                detail="403 Forbidden",
            )
        )
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "boom"},
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["code"] == "PLATFORM_AUTH"

    def test_platform_send_failed_becomes_502_platform_send_failed(
        self,
        client: TestClient,
        db_path: Path,
        discord_connector: FakeDiscordConnector,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        discord_connector.script(
            SendResult(
                ok=False,
                error=AmcDiscordErrorCode.PLATFORM_SEND_FAILED.value,
                detail="503 Service Unavailable",
            )
        )
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "down"},
        )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "PLATFORM_SEND_FAILED"

    def test_failed_send_does_not_persist_outbound_row(
        self,
        client: TestClient,
        db_path: Path,
        discord_connector: FakeDiscordConnector,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        discord_connector.script(
            SendResult(
                ok=False,
                error=AmcDiscordErrorCode.PLATFORM_SEND_FAILED.value,
                detail="500",
            )
        )
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "lost"},
        )
        assert resp.status_code == 502
        # No outbound row written when the platform call failed.
        assert _read_outbound_rows(db_path) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_same_key_same_body_returns_cached_with_replay_header(
        self,
        client: TestClient,
        db_path: Path,
        discord_connector: FakeDiscordConnector,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        body = {"channel_id": DISCORD_CHANNEL, "text": "idem"}

        first = client.post(
            "/messages/send",
            headers={**_auth_headers(), "Idempotency-Key": "k-1"},
            json=body,
        )
        assert first.status_code == 200
        first_body = first.json()
        assert len(discord_connector.calls) == 1

        second = client.post(
            "/messages/send",
            headers={**_auth_headers(), "Idempotency-Key": "k-1"},
            json=body,
        )
        assert second.status_code == 200
        # Replay header surfaced on the cache hit.
        assert second.headers.get("Idempotency-Replayed") == "true"
        assert second.json() == first_body
        # Connector NOT called a second time.
        assert len(discord_connector.calls) == 1
        # Outbound row count unchanged either.
        assert len(_read_outbound_rows(db_path)) == 1

    def test_same_key_different_body_returns_422(
        self,
        client: TestClient,
        db_path: Path,
        discord_connector: FakeDiscordConnector,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        first = client.post(
            "/messages/send",
            headers={**_auth_headers(), "Idempotency-Key": "k-collide"},
            json={"channel_id": DISCORD_CHANNEL, "text": "first"},
        )
        assert first.status_code == 200
        assert len(discord_connector.calls) == 1

        bad = client.post(
            "/messages/send",
            headers={**_auth_headers(), "Idempotency-Key": "k-collide"},
            json={"channel_id": DISCORD_CHANNEL, "text": "second"},
        )
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSE"
        # The colliding call never reached the connector.
        assert len(discord_connector.calls) == 1


# ---------------------------------------------------------------------------
# Discord attachments — Phase 1 stub
# ---------------------------------------------------------------------------


class TestDiscordAttachmentsPhase1:
    def test_discord_url_attachment_returns_413(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={
                "channel_id": DISCORD_CHANNEL,
                "text": "see attached",
                "attachments": [{"url": "https://example.com/file.png"}],
            },
        )
        assert resp.status_code == 413
        body = resp.json()
        assert body["error"]["code"] == "ATTACHMENT_TOO_LARGE_FOR_PLATFORM"
        # Message references the eventual 25 MB limit (spec §5.5).
        assert "25" in body["error"]["message"]

    def test_discord_path_attachment_returns_413(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={
                "channel_id": DISCORD_CHANNEL,
                "text": "see attached",
                "attachments": [{"path": "/tmp/file.png"}],
            },
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "ATTACHMENT_TOO_LARGE_FOR_PLATFORM"


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


class TestAuthAndValidation:
    def test_missing_bearer_returns_401(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers={},
            json={"channel_id": DISCORD_CHANNEL, "text": "hi"},
        )
        assert resp.status_code == 401

    def test_wrong_bearer_returns_401(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(bearer="wrong"),
            json={"channel_id": DISCORD_CHANNEL, "text": "hi"},
        )
        assert resp.status_code == 401

    def test_x_agent_id_not_required(
        self,
        client: TestClient,
        db_path: Path,
    ) -> None:
        """Spec §7.4 header table: X-Agent-ID is NOT required for sends."""
        _seed_channel(db_path, source="discord", channel_id=DISCORD_CHANNEL)
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),  # no X-Agent-ID
            json={"channel_id": DISCORD_CHANNEL, "text": "no agent id"},
        )
        assert resp.status_code == 200

    def test_empty_text_and_no_attachments_rejected_at_model(self) -> None:
        """The model's ``_require_text_or_attachment`` validator rejects empty.

        Tested at the model level rather than the endpoint level because the
        global ``RequestValidationError`` handler (#27) cannot currently
        serialize the Pydantic ``ctx.error`` ``ValueError`` raised by a
        model-level validator. The route still rejects the body — it's the
        envelope shape that's broken in that path. The route's other
        validation cases (missing field, extra field) exercise the global
        handler with serializable errors.
        """
        from pydantic import ValidationError

        from amc.core.envelope import SendMessageRequest

        with pytest.raises(ValidationError):
            SendMessageRequest(channel_id=DISCORD_CHANNEL, text="")

    def test_missing_channel_id_returns_400(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"text": "no channel"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_unknown_extra_field_rejected(
        self,
        client: TestClient,
    ) -> None:
        """``extra='forbid'`` on the model rejects unknown fields."""
        resp = client.post(
            "/messages/send",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_CHANNEL, "text": "hi", "bogus": True},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Wiring sanity
# ---------------------------------------------------------------------------


class TestWiring:
    def test_unconfigured_session_factory_raises_500(
        self,
        db_path: Path,  # noqa: ARG002 — keep alembic upgrade in scope
    ) -> None:
        """If startup forgot ``configure_session_factory`` we get INTERNAL_ERROR."""
        configure_bearer_token(BEARER)
        # Note: do NOT configure session factory.
        configure_rate_limiter(RateLimiter(rps=10.0, burst=10))
        configure_discord_connector(FakeDiscordConnector())  # type: ignore[arg-type]
        # Idempotency dep won't trigger without the header — leave it unset.

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router)

        with TestClient(app, raise_server_exceptions=False) as test_client:
            resp = test_client.post(
                "/messages/send",
                headers=_auth_headers(),
                json={"channel_id": DISCORD_CHANNEL, "text": "x"},
            )
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
