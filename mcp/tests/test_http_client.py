"""Unit tests for ``amg_mcp.http_client`` (mirrors http.test.ts)."""

from __future__ import annotations

import re

import httpx
import pytest

from amg_mcp.config import WrapperConfig
from amg_mcp.http_client import (
    NETWORK_ERROR_STATUS,
    HttpErr,
    HttpOk,
    create_http_client,
    random_idempotency_key,
)

TEST_CONFIG = WrapperConfig(
    base_url="http://127.0.0.1:8080",
    bearer_token="test-token",
    agent_id="claude-code",
)


def _mock_transport(
    handler,
    recorded: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def _wrapped(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(request)
        return handler(request)

    return httpx.MockTransport(_wrapped)


def _client_with(handler, recorded: list[httpx.Request] | None = None):
    transport = _mock_transport(handler, recorded)
    return create_http_client(
        config=TEST_CONFIG,
        client=httpx.AsyncClient(transport=transport, timeout=5.0),
    )


class TestGet:
    @pytest.mark.asyncio
    async def test_sends_get_with_default_headers_and_parses_json(self) -> None:
        recorded: list[httpx.Request] = []
        client = _client_with(
            lambda r: httpx.Response(200, json={"messages": [], "next_since": None}),
            recorded,
        )
        result = await client.get("/messages/unread")
        assert isinstance(result, HttpOk)
        assert result.status == 200
        assert result.data == {"messages": [], "next_since": None}
        assert recorded[0].method == "GET"
        assert str(recorded[0].url) == "http://127.0.0.1:8080/messages/unread"
        assert recorded[0].headers["authorization"] == "Bearer test-token"
        assert recorded[0].headers["x-agent-id"] == "claude-code"
        assert recorded[0].headers["content-type"] == "application/json"
        assert recorded[0].content == b""
        await client.aclose()

    @pytest.mark.asyncio
    async def test_serializes_query_parameters(self) -> None:
        recorded: list[httpx.Request] = []
        client = _client_with(lambda r: httpx.Response(200, json={}), recorded)
        await client.get(
            "/messages/unread",
            {"source": "discord", "limit": 25, "since": "2026-04-25T15:32:11Z"},
        )
        url = recorded[0].url
        assert url.params.get("source") == "discord"
        assert url.params.get("limit") == "25"
        assert url.params.get("since") == "2026-04-25T15:32:11Z"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_skips_none_query_values(self) -> None:
        recorded: list[httpx.Request] = []
        client = _client_with(lambda r: httpx.Response(200, json={}), recorded)
        await client.get(
            "/messages/unread",
            {"source": "discord", "channel_id": None, "since": None},
        )
        url = recorded[0].url
        assert url.params.get("source") == "discord"
        assert "channel_id" not in url.params
        assert "since" not in url.params
        await client.aclose()

    @pytest.mark.asyncio
    async def test_normalizes_path_without_leading_slash(self) -> None:
        recorded: list[httpx.Request] = []
        client = _client_with(lambda r: httpx.Response(200, json={}), recorded)
        await client.get("messages/unread")
        assert str(recorded[0].url) == "http://127.0.0.1:8080/messages/unread"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_strips_trailing_slash_on_base_url(self) -> None:
        recorded: list[httpx.Request] = []
        cfg = WrapperConfig(
            base_url="http://127.0.0.1:8080/",
            bearer_token="t",
            agent_id="a",
        )
        client = create_http_client(
            config=cfg,
            client=httpx.AsyncClient(
                transport=_mock_transport(lambda r: httpx.Response(200, json={}), recorded)
            ),
        )
        await client.get("/messages/unread")
        assert str(recorded[0].url) == "http://127.0.0.1:8080/messages/unread"
        await client.aclose()


class TestPost:
    @pytest.mark.asyncio
    async def test_sends_post_with_json_body_and_default_headers(self) -> None:
        recorded: list[httpx.Request] = []
        client = _client_with(lambda r: httpx.Response(200, json={"marked_count": 2}), recorded)
        result = await client.post(
            "/messages/mark_read", {"message_ids": ["msg_01HXYZ", "msg_01HABC"]}
        )
        assert isinstance(result, HttpOk)
        assert result.data == {"marked_count": 2}
        assert recorded[0].method == "POST"
        import json

        assert json.loads(recorded[0].content) == {"message_ids": ["msg_01HXYZ", "msg_01HABC"]}
        assert recorded[0].headers["content-type"] == "application/json"
        assert recorded[0].headers["authorization"] == "Bearer test-token"
        assert recorded[0].headers["x-agent-id"] == "claude-code"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_does_not_add_idempotency_key_by_default(self) -> None:
        recorded: list[httpx.Request] = []
        client = _client_with(lambda r: httpx.Response(200, json={}), recorded)
        await client.post("/messages/mark_read", {"message_ids": []})
        assert "idempotency-key" not in {k.lower() for k in recorded[0].headers}
        await client.aclose()

    @pytest.mark.asyncio
    async def test_passes_through_caller_idempotency_key(self) -> None:
        recorded: list[httpx.Request] = []
        client = _client_with(
            lambda r: httpx.Response(
                200, json={"message_id": "msg_01HXYZ", "sent_at": "2026-04-25T15:32:13Z"}
            ),
            recorded,
        )
        key = random_idempotency_key()
        await client.post(
            "/messages/send",
            {"channel_id": "+15551234567", "text": "hi"},
            extra_headers={"Idempotency-Key": key},
        )
        assert recorded[0].headers["idempotency-key"] == key
        await client.aclose()

    @pytest.mark.asyncio
    async def test_extra_headers_can_override_defaults(self) -> None:
        recorded: list[httpx.Request] = []
        client = _client_with(lambda r: httpx.Response(200, json={}), recorded)
        await client.post(
            "/messages/send", {}, extra_headers={"Content-Type": "application/x-custom"}
        )
        assert recorded[0].headers["content-type"] == "application/x-custom"
        await client.aclose()


class TestRandomIdempotencyKey:
    def test_returns_uuid_shaped_string(self) -> None:
        key = random_idempotency_key()
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            key,
            re.IGNORECASE,
        )

    def test_returns_fresh_value_each_call(self) -> None:
        assert random_idempotency_key() != random_idempotency_key()


class TestNon2xxHandling:
    @pytest.mark.asyncio
    async def test_maps_envelope_to_http_err(self) -> None:
        body = {"error": {"code": "CHANNEL_NOT_FOUND", "message": "Unknown channel"}}
        client = _client_with(lambda r: httpx.Response(404, json=body))
        result = await client.post("/messages/send", {"channel_id": "x", "text": "hi"})
        assert isinstance(result, HttpErr)
        assert result.status == 404
        assert result.code == "CHANNEL_NOT_FOUND"
        assert result.message == "Unknown channel"
        assert result.body == body
        await client.aclose()

    @pytest.mark.asyncio
    async def test_falls_back_to_internal_error_on_non_envelope_body(self) -> None:
        client = _client_with(lambda r: httpx.Response(500, text="plain text error"))
        result = await client.get("/messages/unread")
        assert isinstance(result, HttpErr)
        assert result.status == 500
        assert result.code == "INTERNAL_ERROR"
        # httpx maps status 500 to "Internal Server Error" reason phrase
        assert "Internal Server Error" in result.message
        assert result.body == "plain text error"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_handles_401_with_envelope(self) -> None:
        body = {"error": {"code": "UNAUTHORIZED", "message": "bad token"}}
        client = _client_with(lambda r: httpx.Response(401, json=body))
        result = await client.get("/messages/unread")
        assert isinstance(result, HttpErr)
        assert result.code == "UNAUTHORIZED"
        assert result.status == 401
        await client.aclose()


class TestNoContent:
    @pytest.mark.asyncio
    async def test_204_returns_ok_with_none_data(self) -> None:
        client = _client_with(lambda r: httpx.Response(204))
        result = await client.post("/typing", {"channel_id": "x"})
        assert isinstance(result, HttpOk)
        assert result.status == 204
        assert result.data is None
        await client.aclose()


class TestNetworkFailures:
    @pytest.mark.asyncio
    async def test_returns_network_error_on_connect_failure(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fetch failed", request=request)

        client = _client_with(boom)
        result = await client.get("/messages/unread")
        assert isinstance(result, HttpErr)
        assert result.code == "NETWORK_ERROR"
        assert result.status == NETWORK_ERROR_STATUS
        assert "fetch failed" in result.message
        await client.aclose()

    @pytest.mark.asyncio
    async def test_returns_network_error_on_timeout(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("operation timed out", request=request)

        client = _client_with(boom)
        result = await client.get("/messages/unread")
        assert isinstance(result, HttpErr)
        assert result.code == "NETWORK_ERROR"
        await client.aclose()
