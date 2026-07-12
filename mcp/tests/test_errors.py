"""Unit tests for ``amg_mcp.errors`` (mirrors errors.test.ts)."""

from __future__ import annotations

import re
from typing import Any

from amg_mcp.config import (
    ENV_AGENT_ID,
    ENV_BASE_URL,
    ENV_BEARER_TOKEN,
    load_config,
    reset_config,
)
from amg_mcp.errors import map_http_error_to_mcp_response
from amg_mcp.http_client import NETWORK_ERROR_STATUS, HttpErr


def _err(**partial: Any) -> HttpErr:
    base = {
        "status": 500,
        "code": "INTERNAL_ERROR",
        "message": "",
        "body": None,
    }
    base.update(partial)
    return HttpErr(**base)


def _text_of(response: Any) -> str:
    assert response.isError is True
    assert len(response.content) == 1
    block = response.content[0]
    assert block["type"] == "text"
    return block["text"]


class TestShape:
    def test_returns_content_array_with_iserror_true(self) -> None:
        r = map_http_error_to_mcp_response(
            _err(status=401, code="UNAUTHORIZED", message="bad token")
        )
        assert r.isError is True
        assert isinstance(r.content, list)
        assert r.content[0]["type"] == "text"
        assert isinstance(r.content[0]["text"], str)


class TestKnownCodes:
    def test_unauthorized(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=401, code="UNAUTHORIZED", message="bad token")
            )
        )
        assert text == "Bearer token rejected by adapter. Check AMG_BEARER_TOKEN."

    def test_agent_id_required(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=400, code="AGENT_ID_REQUIRED", message="missing X-Agent-ID")
            )
        )
        assert text == "Internal error: agent id missing — please report."

    def test_message_not_found(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=404, code="MESSAGE_NOT_FOUND", message="no such msg")
            )
        )
        assert text == "Message not found or quarantined."

    def test_channel_not_found(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=404, code="CHANNEL_NOT_FOUND", message="unknown channel")
            )
        )
        assert text == (
            "Channel not registered. Send to a channel that has received at "
            "least one inbound message."
        )

    def test_idempotency_key_reuse(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=422, code="IDEMPOTENCY_KEY_REUSE", message="reused key")
            )
        )
        assert text == "Internal error: idempotency key collision — please retry."

    def test_rate_limited_with_int_retry_after(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(
                    status=429,
                    code="RATE_LIMITED",
                    message="too many",
                    body={
                        "error": {
                            "code": "RATE_LIMITED",
                            "message": "too many",
                            "details": {"retry_after": 7},
                        }
                    },
                )
            )
        )
        assert text == "Rate limited. Retry after 7 seconds."

    def test_rate_limited_with_string_retry_after(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(
                    status=429,
                    code="RATE_LIMITED",
                    message="too many",
                    body={
                        "error": {
                            "code": "RATE_LIMITED",
                            "message": "too many",
                            "details": {"retry_after": "12"},
                        }
                    },
                )
            )
        )
        assert text == "Rate limited. Retry after 12 seconds."

    def test_rate_limited_falls_back_to_generic(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=429, code="RATE_LIMITED", message="too many")
            )
        )
        assert re.match(r"^Rate limited\.", text)
        assert text.endswith("seconds.")

    def test_platform_send_failed_includes_upstream_message(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(
                    status=502,
                    code="PLATFORM_SEND_FAILED",
                    message="Discord 503 Service Unavailable",
                )
            )
        )
        assert text == "Platform delivery failed: Discord 503 Service Unavailable."

    def test_send_failed_treated_as_platform_send_failed(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=502, code="SEND_FAILED", message="connection reset")
            )
        )
        assert text == "Platform delivery failed: connection reset."

    def test_platform_auth(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=502, code="PLATFORM_AUTH", message="discord token rejected")
            )
        )
        assert text == "Platform authentication failed (e.g., Discord token rejected)."

    def test_attachment_too_large(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(
                    status=413,
                    code="ATTACHMENT_TOO_LARGE_FOR_PLATFORM",
                    message="file is 30MB",
                )
            )
        )
        assert text == "Attachment exceeds platform limit. Compress or omit."

    def test_internal_error_includes_upstream_message(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=500, code="INTERNAL_ERROR", message="unexpected exception")
            )
        )
        assert text == "Adapter internal error: unexpected exception."

    def test_unknown_code_falls_back_to_internal(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(status=503, code="TOTALLY_NEW_CODE", message="mystery failure")
            )
        )
        assert text == "Adapter internal error: mystery failure."


class TestNetworkErrors:
    def test_uses_resolve_base_url_override(self) -> None:
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(
                    status=NETWORK_ERROR_STATUS,
                    code="NETWORK_ERROR",
                    message="fetch failed",
                ),
                resolve_base_url=lambda: "http://192.168.1.10:8080",
            )
        )
        assert text == "Cannot reach adapter at http://192.168.1.10:8080. Is it running?"

    def test_reads_from_get_config_when_no_override(self) -> None:
        load_config(
            {
                ENV_BASE_URL: "http://localhost:9999",
                ENV_BEARER_TOKEN: "t",
                ENV_AGENT_ID: "a",
            }
        )
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(
                    status=NETWORK_ERROR_STATUS,
                    code="NETWORK_ERROR",
                    message="connection refused",
                )
            )
        )
        assert text == "Cannot reach adapter at http://localhost:9999. Is it running?"

    def test_falls_back_when_config_unloaded(self) -> None:
        reset_config()
        text = _text_of(
            map_http_error_to_mcp_response(
                _err(
                    status=NETWORK_ERROR_STATUS,
                    code="NETWORK_ERROR",
                    message="fetch failed",
                )
            )
        )
        assert text == "Cannot reach adapter at AMG_BASE_URL. Is it running?"

    def test_tolerates_throwing_override(self) -> None:
        def _raise() -> str:
            raise RuntimeError("config not loaded")

        text = _text_of(
            map_http_error_to_mcp_response(
                _err(
                    status=NETWORK_ERROR_STATUS,
                    code="NETWORK_ERROR",
                    message="fetch failed",
                ),
                resolve_base_url=_raise,
            )
        )
        assert text == "Cannot reach adapter at AMG_BASE_URL. Is it running?"
