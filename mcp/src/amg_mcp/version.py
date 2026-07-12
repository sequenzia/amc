"""Server identity. Kept in sync with ``pyproject.toml`` via ``test_version_sync``."""

from __future__ import annotations

from typing import Final

SERVER_NAME: Final[str] = "amg-mcp"
SERVER_VERSION: Final[str] = "0.1.0"

__all__ = ["SERVER_NAME", "SERVER_VERSION"]
