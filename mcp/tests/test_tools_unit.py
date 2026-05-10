"""Tool-level unit tests.

Mirrors ``mcp-wrapper/tests/tools/*.test.ts``: each tool is registered on a
fresh FastMCP server bound to a recording fake :class:`HttpClient`, and the
test calls the underlying tool function (via ``mcp.call_tool``) to assert
input handling, request shape, and output translation without involving
stdio or a real adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from amc_mcp.http_client import HttpOk, HttpResult
from amc_mcp.tools import (
    register_get_message_context,
    register_list_unread_messages,
    register_mark_read,
    register_send_message,
)

pytestmark = pytest.mark.asyncio


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, Any] | None
    body: Any
    extra_headers: dict[str, str] | None


@dataclass
class FakeHttpClient:
    response: HttpResult = field(default_factory=lambda: HttpOk(status=200, data={"ok": True}))
    calls: list[RecordedRequest] = field(default_factory=list)

    async def get(self, path: str, query: dict[str, Any] | None = None) -> HttpResult:
        self.calls.append(
            RecordedRequest(method="GET", path=path, query=query, body=None, extra_headers=None)
        )
        return self.response

    async def post(
        self,
        path: str,
        body: Any,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        self.calls.append(
            RecordedRequest(
                method="POST",
                path=path,
                query=None,
                body=body,
                extra_headers=extra_headers,
            )
        )
        return self.response

    async def aclose(self) -> None:
        return None


def _build(tool_register, response: HttpResult | None = None):
    http = FakeHttpClient()
    if response is not None:
        http.response = response
    mcp = FastMCP(name="test-mcp")
    tool_register(mcp, http)
    return mcp, http


def _text_block(result) -> str:
    block = result.content[0]
    assert getattr(block, "type", None) == "text"
    return block.text


# ---------------------------------------------------------------------------
# list_unread_messages
# ---------------------------------------------------------------------------


class TestListUnread:
    async def test_lists_advertise_metadata(self) -> None:
        mcp, _ = _build(register_list_unread_messages)
        tools = await mcp.list_tools()
        names = [t.name for t in tools]
        assert "list_unread_messages" in names

    async def test_omits_unset_filters(self) -> None:
        mcp, http = _build(
            register_list_unread_messages,
            response=HttpOk(status=200, data={"messages": [], "next_since": None}),
        )
        await mcp.call_tool("list_unread_messages", {})
        call = http.calls[-1]
        assert call.method == "GET"
        assert call.path == "/messages/unread"
        assert call.query == {}

    async def test_passes_all_filters(self) -> None:
        mcp, http = _build(
            register_list_unread_messages,
            response=HttpOk(status=200, data={"messages": [], "next_since": None}),
        )
        await mcp.call_tool(
            "list_unread_messages",
            {
                "since": "2026-04-25T00:00:00Z",
                "source": "discord",
                "channel_id": "discord:1",
                "limit": 10,
            },
        )
        call = http.calls[-1]
        assert call.query == {
            "since": "2026-04-25T00:00:00Z",
            "source": "discord",
            "channel_id": "discord:1",
            "limit": 10,
        }

    async def test_normalizes_response_shape(self) -> None:
        mcp, _ = _build(
            register_list_unread_messages,
            response=HttpOk(status=200, data="not a dict"),
        )
        result = await mcp.call_tool("list_unread_messages", {})
        payload = json.loads(_text_block(result))
        assert payload == {"messages": [], "next_since": None}


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    async def test_posts_with_idempotency_key(self) -> None:
        mcp, http = _build(
            register_send_message,
            response=HttpOk(
                status=200,
                data={"message_id": "msg_X", "sent_at": "2026-04-25T15:34:00Z"},
            ),
        )
        await mcp.call_tool(
            "send_message",
            {"channel_id": "+15551234567", "text": "hello"},
        )
        call = http.calls[-1]
        assert call.method == "POST"
        assert call.path == "/messages/send"
        assert call.body == {"channel_id": "+15551234567", "text": "hello"}
        assert call.extra_headers is not None
        assert "Idempotency-Key" in call.extra_headers
        assert len(call.extra_headers["Idempotency-Key"]) == 36  # UUIDv4

    async def test_attachment_xor_violation_rejected(self) -> None:
        mcp, http = _build(register_send_message)
        # Attachment with both url AND path must be rejected by Pydantic.
        # In direct call_tool (no MCP runtime), validation errors raise
        # ToolError; the MCP runtime catches it and converts to isError=true.
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "send_message",
                {
                    "channel_id": "x",
                    "text": "y",
                    "attachments": [{"url": "http://x", "path": "/tmp/x"}],
                },
            )
        assert http.calls == []

    async def test_attachment_neither_field_rejected(self) -> None:
        mcp, http = _build(register_send_message)
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "send_message",
                {"channel_id": "x", "text": "y", "attachments": [{}]},
            )
        assert http.calls == []

    async def test_passes_through_attachments(self) -> None:
        mcp, http = _build(
            register_send_message,
            response=HttpOk(
                status=200,
                data={"message_id": "msg_X", "sent_at": "2026-04-25T15:34:00Z"},
            ),
        )
        await mcp.call_tool(
            "send_message",
            {
                "channel_id": "x",
                "text": "y",
                "attachments": [{"url": "http://x"}, {"path": "/tmp/x.png"}],
            },
        )
        call = http.calls[-1]
        assert call.body == {
            "channel_id": "x",
            "text": "y",
            "attachments": [{"url": "http://x"}, {"path": "/tmp/x.png"}],
        }


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------


class TestMarkRead:
    async def test_posts_message_ids_no_idempotency_key(self) -> None:
        mcp, http = _build(
            register_mark_read,
            response=HttpOk(status=200, data={"marked_count": 1}),
        )
        await mcp.call_tool(
            "mark_read",
            {"message_ids": ["msg_01HXYZABCDEFGHJKMNPQRSTVWX"]},
        )
        call = http.calls[-1]
        assert call.method == "POST"
        assert call.path == "/messages/mark_read"
        assert call.body == {"message_ids": ["msg_01HXYZABCDEFGHJKMNPQRSTVWX"]}
        assert call.extra_headers in (None, {})

    async def test_rejects_malformed_id_before_calling_adapter(self) -> None:
        mcp, http = _build(register_mark_read)
        with pytest.raises(ToolError):
            await mcp.call_tool("mark_read", {"message_ids": ["not-a-msg-id"]})
        assert http.calls == []


# ---------------------------------------------------------------------------
# get_message_context
# ---------------------------------------------------------------------------


class TestGetMessageContext:
    async def test_passes_explicit_before_after(self) -> None:
        mcp, http = _build(
            register_get_message_context,
            response=HttpOk(status=200, data={"messages": []}),
        )
        await mcp.call_tool(
            "get_message_context",
            {
                "channel_id": "x",
                "around_message_id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",
                "before": 3,
                "after": 2,
            },
        )
        call = http.calls[-1]
        assert call.query == {
            "channel_id": "x",
            "around_message_id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",
            "before": 3,
            "after": 2,
        }

    async def test_defaults_before_after_to_5(self) -> None:
        mcp, http = _build(
            register_get_message_context,
            response=HttpOk(status=200, data={"messages": []}),
        )
        await mcp.call_tool(
            "get_message_context",
            {
                "channel_id": "x",
                "around_message_id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",
            },
        )
        call = http.calls[-1]
        assert call.query["before"] == 5
        assert call.query["after"] == 5

    async def test_rejects_malformed_around_message_id(self) -> None:
        mcp, http = _build(register_get_message_context)
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "get_message_context",
                {"channel_id": "x", "around_message_id": "not-a-msg-id"},
            )
        assert http.calls == []

    async def test_rejects_before_above_cap(self) -> None:
        mcp, http = _build(register_get_message_context)
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "get_message_context",
                {
                    "channel_id": "x",
                    "around_message_id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",
                    "before": 999,
                },
            )
        assert http.calls == []
