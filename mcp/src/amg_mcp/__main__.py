"""Entrypoint for the AMG MCP wrapper.

Loads config from env, builds an :class:`HttpClient`, wires up the four
tools on a :class:`FastMCP` server, and runs the stdio transport.
"""

from __future__ import annotations

import sys

from amg_mcp.config import WrapperConfigError, load_config
from amg_mcp.http_client import create_http_client
from amg_mcp.server import build_server


def main() -> None:
    try:
        config = load_config()
    except WrapperConfigError as exc:
        sys.stderr.write(f"[amg-mcp] fatal: {exc}\n")
        sys.exit(1)

    http = create_http_client(config=config)
    server = build_server(http)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
