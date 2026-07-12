"""MCP tool: ``mark_read``.

Spec references:
  - blueprint §6.1 — tool surface (``mark_read`` input/output)
  - blueprint §5.1 / §7.4.4 — ``POST /messages/mark_read``
  - §14 OQ-1 — ``marked_count`` is the count of unique ids submitted, not
               newly inserted rows; the wrapper just forwards the adapter
               response.

``mark_read`` is naturally idempotent at the storage layer (UPSERT on
``(message_id, agent_id)``) so we do NOT attach an ``Idempotency-Key``.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field, StringConstraints

from amg_mcp.http_client import HttpClient
from amg_mcp.tools._helpers import to_call_tool_result
from amg_mcp.tools._ids import MESSAGE_ID

_TOOL_NAME = "mark_read"
_TOOL_DESCRIPTION = (
    "Acknowledge one or more messages so they no longer appear in "
    "`list_unread_messages`. Maps to POST /messages/mark_read. Returns "
    "`{ marked_count }`: the total number of unique ids submitted."
)

_MessageId = Annotated[str, StringConstraints(pattern=MESSAGE_ID)]


def register_mark_read(mcp: FastMCP, http: HttpClient) -> None:
    @mcp.tool(name=_TOOL_NAME, description=_TOOL_DESCRIPTION)
    async def mark_read(
        message_ids: Annotated[
            list[_MessageId],
            Field(description="Message ids (msg_<ULID>) to acknowledge."),
        ],
    ) -> CallToolResult:
        result = await http.post("/messages/mark_read", {"message_ids": message_ids})
        return to_call_tool_result(result)
