"""Shared helpers for tool handlers."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent

from amc_mcp.errors import map_http_error_to_mcp_response
from amc_mcp.http_client import HttpErr, HttpResult


def success_result(data: Any) -> CallToolResult:
    """Wrap a JSON-serializable payload into a single-text-block CallToolResult.

    Mirrors what the TypeScript wrapper did: emit a pretty-printed JSON text
    block AND expose ``structuredContent`` for newer clients (per the
    MCP 2025-06-18 spec).
    """
    text = json.dumps(data, indent=2, default=str)
    structured: dict[str, Any] | None
    structured = data if isinstance(data, dict) else None
    kwargs: dict[str, Any] = {
        "content": [TextContent(type="text", text=text)],
    }
    if structured is not None:
        kwargs["structuredContent"] = structured
    return CallToolResult(**kwargs)


def error_result(err: HttpErr) -> CallToolResult:
    """Translate an :class:`HttpErr` into an MCP error CallToolResult."""
    response = map_http_error_to_mcp_response(err)
    return CallToolResult(
        content=[TextContent(type="text", text=response.content[0]["text"])],
        isError=True,
    )


def to_call_tool_result(result: HttpResult) -> CallToolResult:
    """Dispatch helper: success → JSON text block; HttpErr → mapped error."""
    if isinstance(result, HttpErr):
        return error_result(result)
    return success_result(result.data)
