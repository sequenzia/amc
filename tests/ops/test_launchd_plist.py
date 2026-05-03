"""Static validation of the committed launchd plist (spec §9.4).

The plist at ``ops/launchd/com.user.amc-adapter.plist`` is a *template* containing
two placeholders rendered at install time by ``ops/launchd/install.sh``:

- ``__INSTALL_DIR__`` — absolute path to the repo checkout
- ``__HOME__`` — operator's ``$HOME`` (used to anchor log paths under
  ``~/Library/Logs/messaging-agent/``)

These tests:

1. Run ``plutil -lint`` on the rendered plist (substitutes placeholders with
   known test values). Skipped when ``plutil`` is unavailable (e.g. Linux CI).
2. Parse the rendered plist with stdlib :mod:`plistlib` and assert the keys
   required by spec §9.4 are present.
3. Assert ``Label`` matches ``com.user.amc-adapter``.
4. Assert log paths resolve under ``~/Library/Logs/messaging-agent/`` once
   ``__HOME__`` is expanded.

A live ``launchctl load``/reboot-cycle exercise is explicitly *not* a build
gate per spec §9.4 — that is a deployment-time activity.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLIST_PATH = REPO_ROOT / "ops" / "launchd" / "com.user.amc-adapter.plist"

# Placeholder substitutions for rendering the template into a valid plist.
TEST_INSTALL_DIR = "/opt/amc"
TEST_HOME = "/Users/testuser"

REQUIRED_KEYS = {
    "Label",
    "ProgramArguments",
    "RunAtLoad",
    "KeepAlive",
    "StandardOutPath",
    "StandardErrorPath",
}

EXPECTED_LABEL = "com.user.amc-adapter"
EXPECTED_LOG_DIR_SUFFIX = "Library/Logs/messaging-agent"


def _render_template(raw: str) -> str:
    """Substitute install-time placeholders with deterministic test values."""
    return raw.replace("__INSTALL_DIR__", TEST_INSTALL_DIR).replace("__HOME__", TEST_HOME)


@pytest.fixture(scope="module")
def rendered_plist_bytes() -> bytes:
    raw = PLIST_PATH.read_text(encoding="utf-8")
    return _render_template(raw).encode("utf-8")


@pytest.fixture(scope="module")
def rendered_plist_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("launchd") / "com.user.amc-adapter.plist"
    raw = PLIST_PATH.read_text(encoding="utf-8")
    path.write_text(_render_template(raw), encoding="utf-8")
    return path


def test_plist_template_committed() -> None:
    """The committed plist template exists at the expected path."""
    assert PLIST_PATH.is_file(), f"missing plist template at {PLIST_PATH}"


@pytest.mark.skipif(
    shutil.which("plutil") is None and sys.platform != "darwin",
    reason="plutil unavailable on non-macOS hosts (Linux CI)",
)
def test_plutil_lint_clean(rendered_plist_path: Path) -> None:
    """``plutil -lint`` reports no syntax errors on the rendered plist.

    On macOS CI this must run and pass. On Linux CI ``plutil`` is absent and
    the test skips. We additionally enforce that on darwin ``plutil`` is on
    PATH so accidental masking doesn't silently skip the only OS that ships it.
    """
    plutil = shutil.which("plutil")
    if sys.platform == "darwin":
        assert plutil is not None, "plutil must be available on macOS CI"
    assert plutil is not None  # narrow for mypy/ruff after the skipif guard

    result = subprocess.run(
        [plutil, "-lint", str(rendered_plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"plutil -lint failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # plutil prints "<path>: OK" on success.
    assert "OK" in result.stdout


def test_required_keys_present(rendered_plist_bytes: bytes) -> None:
    """All keys mandated by spec §9.4 are present at the top level."""
    plist = plistlib.loads(rendered_plist_bytes)
    missing = REQUIRED_KEYS - set(plist.keys())
    assert not missing, f"plist missing required keys: {sorted(missing)}"


def test_label_matches_spec(rendered_plist_bytes: bytes) -> None:
    """``Label`` is the canonical ``com.user.amc-adapter``."""
    plist = plistlib.loads(rendered_plist_bytes)
    assert plist["Label"] == EXPECTED_LABEL


def test_run_at_load_true(rendered_plist_bytes: bytes) -> None:
    """``RunAtLoad`` is ``true`` so launchd starts the adapter at login."""
    plist = plistlib.loads(rendered_plist_bytes)
    assert plist["RunAtLoad"] is True


def test_keep_alive_present(rendered_plist_bytes: bytes) -> None:
    """``KeepAlive`` exists and is either bool ``True`` or a dict policy."""
    plist = plistlib.loads(rendered_plist_bytes)
    keep_alive = plist["KeepAlive"]
    assert isinstance(keep_alive, (bool, dict)), (
        f"KeepAlive must be bool or dict, got {type(keep_alive).__name__}"
    )


def test_log_paths_under_messaging_agent_dir(rendered_plist_bytes: bytes) -> None:
    """``StandardOutPath`` and ``StandardErrorPath`` live under
    ``~/Library/Logs/messaging-agent/`` (with ``__HOME__`` template expansion).
    """
    plist = plistlib.loads(rendered_plist_bytes)
    stdout_path = plist["StandardOutPath"]
    stderr_path = plist["StandardErrorPath"]

    # After template rendering the paths must be absolute (start with TEST_HOME)
    # and contain the messaging-agent log directory segment.
    for label, value in (("StandardOutPath", stdout_path), ("StandardErrorPath", stderr_path)):
        assert isinstance(value, str), f"{label} must be a string, got {type(value).__name__}"
        assert value.startswith(TEST_HOME), (
            f"{label} should be anchored under __HOME__ ({TEST_HOME}); got {value!r}"
        )
        assert EXPECTED_LOG_DIR_SUFFIX in value, (
            f"{label} should be under ~/{EXPECTED_LOG_DIR_SUFFIX}/; got {value!r}"
        )

    # Sanity: the two log files must be distinct so stdout/stderr don't collide.
    assert stdout_path != stderr_path


def test_log_paths_template_expands_to_real_home() -> None:
    """The committed template uses ``__HOME__`` which expands to ``$HOME``
    at install time, equivalent to ``~`` per spec §9.4."""
    raw = PLIST_PATH.read_text(encoding="utf-8")
    assert "__HOME__/Library/Logs/messaging-agent/" in raw, (
        "plist template must reference ~/Library/Logs/messaging-agent/ via the __HOME__ placeholder"
    )
    # Confirm the expansion semantics: $HOME is the documented anchor.
    real_home = os.path.expanduser("~")
    assert real_home and real_home != "~", "test environment must define HOME"
