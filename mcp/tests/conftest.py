"""Shared fixtures for the wrapper test suite."""

from __future__ import annotations

import pytest

from amg_mcp.config import reset_config


@pytest.fixture(autouse=True)
def _reset_config_singleton() -> None:
    """Make sure no test leaks loaded config to its neighbour."""
    yield
    reset_config()
