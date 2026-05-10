"""FastMCP server factory.

Builds a :class:`FastMCP` instance and registers the four AMC tools against
an injected :class:`HttpClient`. Keeping the wiring in a factory (rather
than at module load) lets tests pass a stub client without touching env
vars.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from amc_mcp.http_client import HttpClient
from amc_mcp.tools import (
    register_get_message_context,
    register_list_unread_messages,
    register_mark_read,
    register_send_message,
)
from amc_mcp.version import SERVER_NAME, SERVER_VERSION

__all__ = ["build_server"]


def build_server(http: HttpClient) -> FastMCP:
    """Construct a :class:`FastMCP` server with the four tools registered."""
    mcp = FastMCP(name=SERVER_NAME)
    # FastMCP's constructor doesn't expose a ``version`` kwarg, but the
    # underlying lower-level ``Server`` reports it in the ``initialize``
    # handshake. Set it explicitly so MCP hosts see the right server identity.
    mcp._mcp_server.version = SERVER_VERSION
    register_list_unread_messages(mcp, http)
    register_send_message(mcp, http)
    register_mark_read(mcp, http)
    register_get_message_context(mcp, http)
    return mcp
