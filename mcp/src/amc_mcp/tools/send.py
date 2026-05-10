"""MCP tool: ``send_message`` — thin wrapper over POST /messages/send.

Spec references:
  - §6.1 (``send_message`` input/output)
  - §5.1 (POST /messages/send endpoint)
  - §7.4.7 (``Idempotency-Key`` header on POST /messages/send)
  - §7.4.12 (error envelope)

Design contract:
  * Input shape mirrors ``SendMessageRequest`` in ``amc/core/envelope.py``:
    ``channel_id`` and ``text`` are required, ``reply_to`` and ``attachments``
    are optional, and each attachment must specify exactly one of ``url`` or
    ``path``.
  * Generates a fresh ``Idempotency-Key`` per call via
    :func:`random_idempotency_key` (UUID v4). The wrapper itself owns key
    generation so the four-tool surface stays self-contained.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import BaseModel, Field, model_validator

from amc_mcp.http_client import HttpClient, random_idempotency_key
from amc_mcp.tools._helpers import to_call_tool_result


class SendAttachmentInput(BaseModel):
    url: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> SendAttachmentInput:
        if (self.url is None) == (self.path is None):
            raise ValueError("Provide exactly one of url or path")
        return self


_TOOL_NAME = "send_message"
_TOOL_DESCRIPTION = (
    "Send a message to a channel. Optional `reply_to` threads under another "
    "message in the same channel. Each attachment must supply exactly one of "
    "`url` (HTTP(S)) or `path` (local file)."
)


def register_send_message(mcp: FastMCP, http: HttpClient) -> None:
    @mcp.tool(name=_TOOL_NAME, description=_TOOL_DESCRIPTION)
    async def send_message(
        channel_id: Annotated[
            str, Field(description="Target channel id (Discord snowflake or iMessage E.164/GUID).")
        ],
        text: Annotated[str, Field(description="Message body.")],
        reply_to: Annotated[
            str | None,
            Field(default=None, description="Optional message id to thread under."),
        ] = None,
        attachments: Annotated[
            list[SendAttachmentInput] | None,
            Field(default=None, description="Optional list of attachments (each url XOR path)."),
        ] = None,
    ) -> CallToolResult:
        body: dict[str, Any] = {"channel_id": channel_id, "text": text}
        if reply_to is not None:
            body["reply_to"] = reply_to
        if attachments is not None:
            body["attachments"] = [a.model_dump(exclude_none=True) for a in attachments]

        idempotency_key = random_idempotency_key()
        result = await http.post(
            "/messages/send",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return to_call_tool_result(result)
