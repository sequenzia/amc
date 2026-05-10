"""End-to-end harness — stdio_client → real amc-mcp subprocess → mock adapter.

Mirrors ``mcp-wrapper/tests/e2e.test.ts``: covers the ``initialize``
handshake, ``tools/list``, every ``tools/call`` happy path, the §7.4.12
error-mapping path, the malformed-input rejection path, and the composite
agent walkthrough (list → context → send → mark) inside one MCP session.

The wrapper binary is launched via ``stdio_client`` (the official MCP
Python SDK harness), so this also covers what ``smoke.test.ts`` did
in TypeScript: real stdio framing, real subprocess.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from _mock_adapter import MockAdapter, json_canned
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import InitializeResult

pytestmark = pytest.mark.asyncio


SERVER_NAME = "amc-mcp"
SERVER_VERSION = "0.1.0"

DISCORD_ENVELOPE: dict[str, Any] = {
    "id": "msg_01HXYZABCDEFGHJKMNPQRSTVWX",
    "source": "discord",
    "channel_id": "discord:1234567890",
    "channel_type": "dm",
    "sender": {
        "id": "discord:user:9999",
        "display_name": "Alice",
        "person_id": "alice",
    },
    "text": "hey from discord",
    "attachments": [],
    "reply_to": None,
    "timestamp": "2026-04-25T15:32:11Z",
    "direction": "inbound",
    "raw": {"snowflake": "1234567890"},
}

IMESSAGE_ENVELOPE: dict[str, Any] = {
    "id": "msg_01HABCDEFGHJKMNPQRSTVWXYZ0",
    "source": "imessage",
    "channel_id": "+15551234567",
    "channel_type": "dm",
    "sender": {
        "id": "+15551234567",
        "display_name": "Bob",
        "person_id": None,
    },
    "text": "hey from iMessage",
    "attachments": [
        {
            "id": "att_01HABCDEFGHJKMNPQRSTVWXYZ0",
            "url": "http://127.0.0.1:8080/attachments/att_01HABCDEFGHJKMNPQRSTVWXYZ0",
            "mime": "image/png",
            "size_bytes": 1024,
        }
    ],
    "reply_to": None,
    "timestamp": "2026-04-25T15:33:00Z",
    "direction": "inbound",
    "raw": {"rowid": 42},
}

BEARER_TOKEN = "test-bearer-token"
AGENT_ID = "test-agent"


def _amc_mcp_command() -> tuple[str, list[str]]:
    """Locate the wrapper binary and return (command, extra args)."""
    bin_path = Path(sys.prefix) / "bin" / "amc-mcp"
    if bin_path.exists():
        return str(bin_path), []
    on_path = shutil.which("amc-mcp")
    if on_path:
        return on_path, []
    raise RuntimeError("amc-mcp binary not found — run `uv sync --all-packages` from the repo root")


@pytest.fixture()
def adapter() -> MockAdapter:
    a = MockAdapter()
    a.start()
    try:
        yield a
    finally:
        a.stop()


@asynccontextmanager
async def _open_session(
    adapter: MockAdapter,
) -> AsyncIterator[tuple[ClientSession, InitializeResult]]:
    command, extra_args = _amc_mcp_command()
    params = StdioServerParameters(
        command=command,
        args=extra_args,
        env={
            "AMC_BASE_URL": adapter.base_url,
            "AMC_BEARER_TOKEN": BEARER_TOKEN,
            "AMC_AGENT_ID": AGENT_ID,
            "PATH": os.environ.get("PATH", ""),
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        yield session, init


def _first_text(result) -> str:
    block = result.content[0]
    assert getattr(block, "type", None) == "text"
    return block.text


def _parse_first_text(result) -> Any:
    return json.loads(_first_text(result))


# ---------------------------------------------------------------------------
# initialize handshake
# ---------------------------------------------------------------------------


class TestInitialize:
    async def test_reports_wrapper_server_identity(self, adapter) -> None:
        async with _open_session(adapter) as (_, init):
            assert init.serverInfo.name == SERVER_NAME
            assert init.serverInfo.version == SERVER_VERSION

    async def test_advertises_tools_capability(self, adapter) -> None:
        async with _open_session(adapter) as (_, init):
            assert init.capabilities.tools is not None


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


class TestToolsList:
    async def test_returns_exactly_the_four_amc_tools(self, adapter) -> None:
        async with _open_session(adapter) as (session, _):
            listing = await session.list_tools()
            names = sorted(t.name for t in listing.tools)
            assert names == sorted(
                [
                    "get_message_context",
                    "list_unread_messages",
                    "mark_read",
                    "send_message",
                ]
            )
            for tool in listing.tools:
                assert isinstance(tool.description, str) and tool.description
                assert tool.inputSchema is not None


# ---------------------------------------------------------------------------
# list_unread_messages
# ---------------------------------------------------------------------------


class TestListUnread:
    async def test_forwards_filters_and_returns_discord_envelope(
        self, adapter: MockAdapter
    ) -> None:
        adapter.enqueue(
            "/messages/unread",
            json_canned({"messages": [DISCORD_ENVELOPE], "next_since": "2026-04-25T15:33:00Z"}),
        )
        async with _open_session(adapter) as (session, _):
            result = await session.call_tool(
                "list_unread_messages", {"source": "discord", "limit": 25}
            )

        call = adapter.calls[-1]
        assert call.method == "GET"
        assert call.path == "/messages/unread"
        assert call.query.get("source") == ["discord"]
        assert call.query.get("limit") == ["25"]
        assert call.headers["authorization"] == f"Bearer {BEARER_TOKEN}"
        assert call.headers["x-agent-id"] == AGENT_ID

        payload = _parse_first_text(result)
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["id"] == DISCORD_ENVELOPE["id"]
        assert payload["messages"][0]["source"] == "discord"
        assert payload["next_since"] == "2026-04-25T15:33:00Z"

    async def test_relays_imessage_envelope(self, adapter: MockAdapter) -> None:
        adapter.enqueue(
            "/messages/unread",
            json_canned({"messages": [IMESSAGE_ENVELOPE], "next_since": None}),
        )
        async with _open_session(adapter) as (session, _):
            result = await session.call_tool("list_unread_messages", {"source": "imessage"})
        call = adapter.calls[-1]
        assert call.query.get("source") == ["imessage"]
        payload = _parse_first_text(result)
        assert payload["messages"][0]["channel_id"] == "+15551234567"
        assert payload["messages"][0]["source"] == "imessage"
        assert len(payload["messages"][0]["attachments"]) == 1
        assert payload["messages"][0]["attachments"][0]["id"].startswith("att_")
        assert payload["next_since"] is None


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    async def test_posts_with_idempotency_key_and_returns_payload(
        self, adapter: MockAdapter
    ) -> None:
        adapter.enqueue(
            "/messages/send",
            json_canned(
                {
                    "message_id": "msg_01HZZZABCDEFGHJKMNPQRSTVWX",
                    "sent_at": "2026-04-25T15:34:00Z",
                }
            ),
        )
        async with _open_session(adapter) as (session, _):
            result = await session.call_tool(
                "send_message",
                {"channel_id": "discord:1234567890", "text": "hello back"},
            )
        call = adapter.calls[-1]
        assert call.method == "POST"
        assert call.path == "/messages/send"
        assert json.loads(call.body) == {
            "channel_id": "discord:1234567890",
            "text": "hello back",
        }
        idem = call.headers.get("idempotency-key")
        assert idem is not None
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            idem,
            re.IGNORECASE,
        )

        payload = _parse_first_text(result)
        assert payload["message_id"] == "msg_01HZZZABCDEFGHJKMNPQRSTVWX"
        assert payload["sent_at"] == "2026-04-25T15:34:00Z"

    async def test_imessage_channel_hits_same_endpoint(self, adapter: MockAdapter) -> None:
        adapter.enqueue(
            "/messages/send",
            json_canned(
                {
                    "message_id": "msg_01HZZZABCDEFGHJKMNPQRSTVWY",
                    "sent_at": "2026-04-25T15:35:00Z",
                }
            ),
        )
        async with _open_session(adapter) as (session, _):
            await session.call_tool(
                "send_message",
                {"channel_id": "+15551234567", "text": "reply via iMessage"},
            )
        call = adapter.calls[-1]
        assert json.loads(call.body) == {
            "channel_id": "+15551234567",
            "text": "reply via iMessage",
        }
        assert call.headers["authorization"] == f"Bearer {BEARER_TOKEN}"
        assert call.headers["x-agent-id"] == AGENT_ID

    async def test_surfaces_adapter_error_envelope_as_iserror(self, adapter: MockAdapter) -> None:
        adapter.enqueue(
            "/messages/send",
            json_canned(
                {"error": {"code": "CHANNEL_NOT_FOUND", "message": "Unknown channel"}},
                status=404,
            ),
        )
        async with _open_session(adapter) as (session, _):
            result = await session.call_tool(
                "send_message",
                {"channel_id": "discord:does-not-exist", "text": "hi"},
            )
        assert result.isError is True
        text = _first_text(result)
        assert "Channel not registered" in text


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------


class TestMarkRead:
    async def test_posts_message_ids_and_returns_marked_count(self, adapter: MockAdapter) -> None:
        adapter.enqueue("/messages/mark_read", json_canned({"marked_count": 2}))
        async with _open_session(adapter) as (session, _):
            result = await session.call_tool(
                "mark_read",
                {"message_ids": [DISCORD_ENVELOPE["id"], IMESSAGE_ENVELOPE["id"]]},
            )
        call = adapter.calls[-1]
        assert call.method == "POST"
        assert call.path == "/messages/mark_read"
        assert json.loads(call.body) == {
            "message_ids": [DISCORD_ENVELOPE["id"], IMESSAGE_ENVELOPE["id"]]
        }
        # Mark_read is idempotent at storage; no Idempotency-Key header.
        assert "idempotency-key" not in call.headers

        payload = _parse_first_text(result)
        assert payload["marked_count"] == 2

    async def test_rejects_malformed_message_ids_at_wrapper_boundary(
        self, adapter: MockAdapter
    ) -> None:
        async with _open_session(adapter) as (session, _):
            result = await session.call_tool("mark_read", {"message_ids": ["not-a-msg-id"]})
        assert result.isError is True
        # Adapter must NOT have been called.
        mark_read_calls = [c for c in adapter.calls if c.path == "/messages/mark_read"]
        assert mark_read_calls == []


# ---------------------------------------------------------------------------
# get_message_context
# ---------------------------------------------------------------------------


class TestGetMessageContext:
    async def test_forwards_all_params(self, adapter: MockAdapter) -> None:
        adapter.enqueue("/messages/context", json_canned({"messages": [DISCORD_ENVELOPE]}))
        async with _open_session(adapter) as (session, _):
            result = await session.call_tool(
                "get_message_context",
                {
                    "channel_id": "discord:1234567890",
                    "around_message_id": DISCORD_ENVELOPE["id"],
                    "before": 3,
                    "after": 2,
                },
            )
        call = adapter.calls[-1]
        assert call.method == "GET"
        assert call.path == "/messages/context"
        assert call.query["channel_id"] == ["discord:1234567890"]
        assert call.query["around_message_id"] == [DISCORD_ENVELOPE["id"]]
        assert call.query["before"] == ["3"]
        assert call.query["after"] == ["2"]

        payload = _parse_first_text(result)
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["id"] == DISCORD_ENVELOPE["id"]

    async def test_applies_default_before_after(self, adapter: MockAdapter) -> None:
        adapter.enqueue("/messages/context", json_canned({"messages": [IMESSAGE_ENVELOPE]}))
        async with _open_session(adapter) as (session, _):
            await session.call_tool(
                "get_message_context",
                {
                    "channel_id": "+15551234567",
                    "around_message_id": IMESSAGE_ENVELOPE["id"],
                },
            )
        call = adapter.calls[-1]
        assert call.query["before"] == ["5"]
        assert call.query["after"] == ["5"]


# ---------------------------------------------------------------------------
# Composite agent walkthrough (single session, four tools)
# ---------------------------------------------------------------------------


class TestComposite:
    async def test_full_agent_loop_in_one_session(self, adapter: MockAdapter) -> None:
        adapter.enqueue(
            "/messages/unread",
            json_canned({"messages": [DISCORD_ENVELOPE], "next_since": "2026-04-25T15:33:00Z"}),
        )
        async with _open_session(adapter) as (session, _):
            unread = await session.call_tool("list_unread_messages", {})
            unread_payload = _parse_first_text(unread)
            assert len(unread_payload["messages"]) == 1
            msg_id = unread_payload["messages"][0]["id"]
            channel_id = unread_payload["messages"][0]["channel_id"]

            adapter.enqueue("/messages/context", json_canned({"messages": [DISCORD_ENVELOPE]}))
            await session.call_tool(
                "get_message_context",
                {"channel_id": channel_id, "around_message_id": msg_id},
            )

            adapter.enqueue(
                "/messages/send",
                json_canned(
                    {
                        "message_id": "msg_01HZZZABCDEFGHJKMNPQRSTVWZ",
                        "sent_at": "2026-04-25T15:36:00Z",
                    }
                ),
            )
            await session.call_tool("send_message", {"channel_id": channel_id, "text": "on it"})

            adapter.enqueue("/messages/mark_read", json_canned({"marked_count": 1}))
            ack = await session.call_tool("mark_read", {"message_ids": [msg_id]})
            assert _parse_first_text(ack)["marked_count"] == 1

        paths = [c.path for c in adapter.calls]
        assert paths == [
            "/messages/unread",
            "/messages/context",
            "/messages/send",
            "/messages/mark_read",
        ]
        for call in adapter.calls:
            assert call.headers["authorization"] == f"Bearer {BEARER_TOKEN}"
            assert call.headers["x-agent-id"] == AGENT_ID
