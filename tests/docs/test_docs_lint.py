"""Pytest wrapper around `scripts/docs_lint.py`.

Runs the linter as a subprocess and asserts a clean exit. Failures from the
linter are surfaced verbatim in the assertion message so CI logs identify the
exact `path:line` that needs attention.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LINTER = REPO_ROOT / "scripts" / "docs_lint.py"


def test_linter_script_exists() -> None:
    assert LINTER.is_file(), f"docs_lint.py not found at {LINTER}"


def test_docs_lint_passes() -> None:
    """Run the linter; expect exit code 0 with no errors."""
    result = subprocess.run(
        [sys.executable, str(LINTER)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        pytest.fail(
            "docs_lint.py reported errors:\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
