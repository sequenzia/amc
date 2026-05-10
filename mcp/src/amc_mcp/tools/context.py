"""MCP tool: ``get_message_context``.

Spec references:
  - §5.4 — feature definition (US-004) and acceptance criteria
  - §7.4.3 — ``GET /messages/context?channel_id=&around_message_id=&before=&after=``
  - §7.4.7 — MCP tool surface (this is one of the four)

Pure shim over the adapter HTTP API. Defaults (``before=5``, ``after=5``)
and bounds (``<=50``) are enforced here so we fail fast with a clear
validation error instead of waiting for the adapter to return 422.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field, StringConstraints

from amc_mcp.http_client import HttpClient
from amc_mcp.tools._helpers import to_call_tool_result
from amc_mcp.tools._ids import MESSAGE_ID

_TOOL_NAME = "get_message_context"
_TOOL_DESCRIPTION = (
    "Fetch N messages before and after a target message in a channel, in "
    "chronological order. Maps to GET /messages/context. Defaults: "
    "before=5, after=5. Caps: before<=50, after<=50."
)

_MessageId = Annotated[str, StringConstraints(pattern=MESSAGE_ID)]


def register_get_message_context(mcp: FastMCP, http: HttpClient) -> None:
    @mcp.tool(name=_TOOL_NAME, description=_TOOL_DESCRIPTION)
    async def get_message_context(
        channel_id: Annotated[str, Field(description="Channel id to scope the lookup.")],
        around_message_id: Annotated[
            _MessageId,
            Field(description="Target message id to look around."),
        ],
        before: Annotated[
            int,
            Field(default=5, ge=0, le=50, description="Messages to return before the target."),
        ] = 5,
        after: Annotated[
            int,
            Field(default=5, ge=0, le=50, description="Messages to return after the target."),
        ] = 5,
    ) -> CallToolResult:
        query: dict[str, Any] = {
            "channel_id": channel_id,
            "around_message_id": around_message_id,
            "before": before,
            "after": after,
        }
        result = await http.get("/messages/context", query)
        return to_call_tool_result(result)
