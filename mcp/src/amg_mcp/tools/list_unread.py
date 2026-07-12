"""MCP tool: ``list_unread_messages``.

Spec references:
  - blueprint §6.1 — tool surface (since / source / channel_id / limit)
  - blueprint §5.1 — adapter REST: GET /messages/unread

Design contract:
  * Self-contained module — exports a single ``register_list_unread_messages``
    function that takes a FastMCP server and an :class:`HttpClient` and wires
    the tool up.
  * No platform-specific code (no discord/applescript/chat.db).
  * Query params are built by omitting any None field so the adapter sees
    exactly what the agent supplied — never ``?source=null``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from amg_mcp.http_client import HttpClient, HttpErr
from amg_mcp.tools._helpers import error_result, success_result

LIST_UNREAD_SOURCES = ("imessage", "discord")

_TOOL_NAME = "list_unread_messages"
_TOOL_DESCRIPTION = (
    "Return allowlisted messages the agent has not yet acknowledged. "
    "Optional filters: source, channel_id, since (ISO 8601), limit."
)


def _normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"messages": [], "next_since": None}
    messages = raw.get("messages")
    next_since = raw.get("next_since")
    if not isinstance(messages, list):
        messages = []
    if not isinstance(next_since, str) and next_since is not None:
        next_since = None
    return {"messages": messages, "next_since": next_since}


def register_list_unread_messages(mcp: FastMCP, http: HttpClient) -> None:
    @mcp.tool(name=_TOOL_NAME, description=_TOOL_DESCRIPTION)
    async def list_unread_messages(
        since: Annotated[
            str | None,
            Field(
                default=None,
                description="ISO 8601 timestamp; only return messages after this point.",
            ),
        ] = None,
        source: Annotated[
            Literal["imessage", "discord"] | None,
            Field(default=None, description="Restrict to a single platform."),
        ] = None,
        channel_id: Annotated[
            str | None,
            Field(default=None, description="Restrict to one channel."),
        ] = None,
        limit: Annotated[
            int | None,
            Field(default=None, ge=1, le=100, description="Max messages to return (1-100)."),
        ] = None,
    ) -> CallToolResult:
        query: dict[str, Any] = {}
        if since is not None:
            query["since"] = since
        if source is not None:
            query["source"] = source
        if channel_id is not None:
            query["channel_id"] = channel_id
        if limit is not None:
            query["limit"] = limit

        result = await http.get("/messages/unread", query)
        if isinstance(result, HttpErr):
            return error_result(result)
        return success_result(_normalize(result.data))
