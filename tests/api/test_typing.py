"""Tests for ``POST /typing`` (spec §5.1, §7.4.6).

Covers:

* Functional: 204 with empty body; Discord ``trigger_typing`` invoked when
  the channel id has the ``discord:`` prefix; iMessage path is a no-op.
* Edge cases: unknown channel returns 204 (best-effort, do NOT 404);
  Discord client not configured → 204 no-op.
* Error handling: connector raises → 204 still returned; warning logged.
* Auth: missing bearer → 401; X-Agent-ID is NOT required.
* Validation: missing / blank ``channel_id`` → 400 ``VALIDATION_FAILED``.

The tests inject a tiny in-process :class:`FakeDiscordTypingClient` rather
than spinning up the gateway harness — the route's contract with the
connector is the small ``trigger_typing(channel_id)`` async surface, and
that is exactly what we exercise.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amc.api.typing import (
    DiscordTypingClient,
    TypingRequest,
    configure_discord_typing_client,
    reset_discord_typing_client,
    router,
)
from amc.core.auth import configure_bearer_token, reset_bearer_token
from amc.core.errors import register_exception_handlers

BEARER = "test-bearer-typing-token"

DISCORD_DM_CHANNEL = "discord:dm:1234567890123456"
DISCORD_GUILD_CHANNEL = "discord:channel:9999999999999999"
IMESSAGE_PHONE_CHANNEL = "+15551234567"
IMESSAGE_GUID_CHANNEL = "iMessage;-;+15551234567"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeDiscordTypingClient:
    """Records every call and optionally raises a queued exception.

    Implements the :class:`DiscordTypingClient` Protocol structurally — the
    route's contract is just ``async def trigger_typing(channel_id) -> None``.
    """

    calls: list[str] = field(default_factory=list)
    next_error: BaseException | None = None

    async def trigger_typing(self, channel_id: str) -> None:
        self.calls.append(channel_id)
        if self.next_error is not None:
            err = self.next_error
            # One-shot: clear so subsequent calls succeed unless re-armed.
            self.next_error = None
            raise err


def test_fake_typing_client_satisfies_protocol() -> None:
    """Sanity: the fake structurally satisfies the runtime-checkable Protocol."""
    client: DiscordTypingClient = FakeDiscordTypingClient()
    assert isinstance(client, DiscordTypingClient)


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_globals() -> Iterator[None]:
    """Clear module-level caches between tests for true isolation."""
    reset_bearer_token()
    reset_discord_typing_client()
    yield
    reset_bearer_token()
    reset_discord_typing_client()


@pytest.fixture
def discord_typing_client() -> FakeDiscordTypingClient:
    return FakeDiscordTypingClient()


@pytest.fixture
def app(discord_typing_client: FakeDiscordTypingClient) -> Iterator[FastAPI]:
    configure_bearer_token(BEARER)
    configure_discord_typing_client(discord_typing_client)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    yield app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _auth_headers(*, bearer: str | None = BEARER) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


class TestTypingRequestModel:
    def test_accepts_channel_id(self) -> None:
        req = TypingRequest(channel_id=DISCORD_DM_CHANNEL)
        assert req.channel_id == DISCORD_DM_CHANNEL

    def test_blank_channel_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            TypingRequest(channel_id="")

    def test_missing_channel_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            TypingRequest()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Functional — Discord branch
# ---------------------------------------------------------------------------


class TestFunctionalDiscord:
    def test_discord_dm_returns_204_with_empty_body(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_DM_CHANNEL},
        )

        assert resp.status_code == 204
        # 204 must not have a body (RFC 9110 §15.3.5).
        assert resp.content == b""
        # The connector got the call.
        assert discord_typing_client.calls == [DISCORD_DM_CHANNEL]

    def test_discord_guild_channel_invokes_trigger_typing(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_GUILD_CHANNEL},
        )

        assert resp.status_code == 204
        assert resp.content == b""
        assert discord_typing_client.calls == [DISCORD_GUILD_CHANNEL]

    def test_repeated_calls_each_invoke_connector(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        for _ in range(3):
            resp = client.post(
                "/typing",
                headers=_auth_headers(),
                json={"channel_id": DISCORD_DM_CHANNEL},
            )
            assert resp.status_code == 204

        assert discord_typing_client.calls == [DISCORD_DM_CHANNEL] * 3


# ---------------------------------------------------------------------------
# Functional — iMessage Phase 2 placeholder (no-op)
# ---------------------------------------------------------------------------


class TestFunctionalIMessage:
    def test_imessage_phone_channel_is_noop_returns_204(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": IMESSAGE_PHONE_CHANNEL},
        )

        assert resp.status_code == 204
        assert resp.content == b""
        # Discord connector NOT called for iMessage channel ids.
        assert discord_typing_client.calls == []

    def test_imessage_chat_guid_channel_is_noop_returns_204(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": IMESSAGE_GUID_CHANNEL},
        )

        assert resp.status_code == 204
        assert resp.content == b""
        assert discord_typing_client.calls == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_unknown_channel_still_returns_204_not_404(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        """Best-effort: even if the channel id has never been observed, no 404.

        The route deliberately skips the channels-table lookup; the connector
        gets the call and may itself fail (silently, swallowed). 204 always.
        """
        unknown_channel = "discord:dm:0000000000000001"
        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": unknown_channel},
        )

        assert resp.status_code == 204
        assert resp.content == b""
        # Connector was still invoked — whether it succeeds is its problem.
        assert discord_typing_client.calls == [unknown_channel]

    def test_discord_client_not_configured_returns_204_noop(
        self,
        discord_typing_client: FakeDiscordTypingClient,  # noqa: ARG002
    ) -> None:
        """When configure_discord_typing_client(None) is called explicitly."""
        configure_bearer_token(BEARER)
        configure_discord_typing_client(None)

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router)

        with TestClient(app) as test_client:
            resp = test_client.post(
                "/typing",
                headers=_auth_headers(),
                json={"channel_id": DISCORD_DM_CHANNEL},
            )

        assert resp.status_code == 204
        assert resp.content == b""

    def test_discord_client_never_configured_returns_204_noop(self) -> None:
        """No configure call at all — startup forgot to wire it.

        The route still returns 204 (best-effort). This is the only piece of
        the API that should be tolerant of missing wiring; everywhere else a
        missing factory raises at request time.
        """
        configure_bearer_token(BEARER)
        # NB: explicitly do NOT call configure_discord_typing_client.

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router)

        with TestClient(app) as test_client:
            resp = test_client.post(
                "/typing",
                headers=_auth_headers(),
                json={"channel_id": DISCORD_DM_CHANNEL},
            )

        assert resp.status_code == 204
        assert resp.content == b""


# ---------------------------------------------------------------------------
# Error handling — connector exception is swallowed
# ---------------------------------------------------------------------------


class TestErrorSwallowing:
    def test_connector_exception_swallowed_returns_204(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        discord_typing_client.next_error = RuntimeError("discord 503 server unavailable")

        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_DM_CHANNEL},
        )

        assert resp.status_code == 204
        assert resp.content == b""
        assert discord_typing_client.calls == [DISCORD_DM_CHANNEL]

    def test_connector_exception_logged_at_warn(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        """Per task: connector errors are logged at WARN, never ERROR.

        Uses ``structlog.testing.capture_logs`` because the project routes
        log events through structlog (see ``amc/core/logging.py``); stdlib
        ``caplog`` only catches the bridged handler when
        ``configure_logging()`` has installed it, which the test app does
        not.
        """
        discord_typing_client.next_error = RuntimeError("boom")

        with structlog.testing.capture_logs() as logs:
            resp = client.post(
                "/typing",
                headers=_auth_headers(),
                json={"channel_id": DISCORD_DM_CHANNEL},
            )

        assert resp.status_code == 204
        # The captured logs are dicts with ``log_level`` and ``event`` keys.
        warns = [
            entry
            for entry in logs
            if entry.get("log_level") == "warning"
            and entry.get("event") == "typing_discord_failed"
        ]
        assert warns, (
            f"expected a WARN structlog event 'typing_discord_failed'; "
            f"captured: {logs!r}"
        )
        # The captured event should include the channel_id and error info.
        assert warns[0].get("channel_id") == DISCORD_DM_CHANNEL
        assert warns[0].get("error_type") == "RuntimeError"

    def test_connector_exception_does_not_block_subsequent_calls(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        # First call raises (one-shot), second call succeeds normally.
        discord_typing_client.next_error = RuntimeError("transient")

        first = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_DM_CHANNEL},
        )
        second = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": DISCORD_DM_CHANNEL},
        )

        assert first.status_code == 204
        assert second.status_code == 204
        # Both calls reached the connector even though the first raised.
        assert discord_typing_client.calls == [DISCORD_DM_CHANNEL, DISCORD_DM_CHANNEL]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_bearer_returns_401(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers={},
            json={"channel_id": DISCORD_DM_CHANNEL},
        )
        assert resp.status_code == 401

    def test_wrong_bearer_returns_401(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers=_auth_headers(bearer="wrong-token"),
            json={"channel_id": DISCORD_DM_CHANNEL},
        )
        assert resp.status_code == 401

    def test_x_agent_id_not_required(
        self,
        client: TestClient,
        discord_typing_client: FakeDiscordTypingClient,
    ) -> None:
        """Spec §7.4 header table: ``POST /typing`` does NOT require X-Agent-ID."""
        resp = client.post(
            "/typing",
            headers=_auth_headers(),  # no X-Agent-ID
            json={"channel_id": DISCORD_DM_CHANNEL},
        )

        assert resp.status_code == 204
        assert discord_typing_client.calls == [DISCORD_DM_CHANNEL]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_channel_id_returns_400_validation_failed(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={},
        )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_blank_channel_id_returns_400_validation_failed(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": ""},
        )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"

    def test_channel_id_wrong_type_returns_400_validation_failed(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/typing",
            headers=_auth_headers(),
            json={"channel_id": 12345},
        )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"
