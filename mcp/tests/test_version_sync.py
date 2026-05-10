"""Assert ``amc_mcp.version`` matches ``pyproject.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path

from amc_mcp.version import SERVER_NAME, SERVER_VERSION


def _project_meta() -> dict:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        return tomllib.load(fh)["project"]


def test_server_name_matches_pyproject() -> None:
    assert _project_meta()["name"] == SERVER_NAME


def test_server_version_matches_pyproject() -> None:
    assert _project_meta()["version"] == SERVER_VERSION
