"""Tests for ``amg.cli.plist`` — the plist renderer module.

Spec references:
* §5.3 Install — read template, substitute ``__INSTALL_DIR__`` /
  ``__HOME__``, ``plutil -lint``, atomic ``mv``. On lint failure leave
  temp file in place.
* §6.2 Security — substitution surface is limited to those two
  placeholders; other ``__X__`` tokens must remain untouched.
* §7.1 Architecture — ``amg/cli/plist.py`` is the home of this pipeline.

Coverage:
* Functional: only ``__INSTALL_DIR__`` and ``__HOME__`` are substituted;
  other ``__OTHER__`` tokens remain intact.
* Functional: ``plutil -lint`` is invoked before any move.
* Functional: ``dest`` is atomically replaced (we observe the final
  bytes; partial writes cannot appear at ``dest``).
* Functional: idempotent rendering — running ``render_plist`` twice with
  identical inputs produces byte-identical output.
* Edge Case: missing template → ``FileNotFoundError`` with the canonical
  ``Plist template not found: <path>`` message.
* Edge Case: ``LaunchAgents/`` parent dir missing → ``install_plist``
  creates it (with mode 0o700).
* Error Handling: lint failure → ``PlistLintError`` carrying the
  retained temp-file path; CLI maps to exit 2.
"""

from __future__ import annotations

import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from amg.cli.plist import (
    PlistLintError,
    install_plist,
    lint_plist,
    render_plist,
)
from amg.cli.services import REGISTRY, Service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEMPLATE_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.amg-test</string>
    <key>WorkingDirectory</key>
    <string>__INSTALL_DIR__</string>
    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/messaging-agent/launchd-stdout.log</string>
    <key>Other</key>
    <string>__OTHER__ should remain</string>
    <key>EnvKeep</key>
    <string>__ENV_NOT_SUBSTITUTED__</string>
</dict>
</plist>
"""


def _make_template(tmp_path: Path, body: str = _TEMPLATE_BODY) -> Path:
    """Write a template file under tmp_path and return its path."""
    p = tmp_path / "template.plist"
    p.write_text(body, encoding="utf-8")
    return p


def _make_service(template_path: Path) -> Service:
    """Build a Service record pointing at ``template_path``."""
    # Reuse the adapter shape so we don't reimplement field-by-field.
    adapter = REGISTRY[0]
    return replace(adapter, plist_template=template_path)


def _fake_run_factory(
    captured: list[list[str]],
    *,
    returncode: int = 0,
    stderr: str = "",
):
    """Return a fake ``subprocess.run`` that records its argv."""

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=returncode,
            stdout="",
            stderr=stderr,
        )

    return _fake_run


# ---------------------------------------------------------------------------
# render_plist — substitution semantics
# ---------------------------------------------------------------------------


def test_render_substitutes_install_dir_and_home(tmp_path: Path) -> None:
    template = _make_template(tmp_path)
    service = _make_service(template)

    out = render_plist(
        service,
        install_dir=Path("/opt/amg"),
        home=Path("/Users/operator"),
    )

    assert "/opt/amg" in out
    assert "/Users/operator/Library/Logs/messaging-agent/launchd-stdout.log" in out
    # Placeholders must be gone.
    assert "__INSTALL_DIR__" not in out
    assert "__HOME__" not in out


def test_render_leaves_other_double_underscore_tokens_alone(tmp_path: Path) -> None:
    template = _make_template(tmp_path)
    service = _make_service(template)

    out = render_plist(
        service,
        install_dir=Path("/opt/amg"),
        home=Path("/Users/op"),
    )

    # Only the two documented placeholders are substituted. Other
    # ``__X__`` shapes must remain in the output.
    assert "__OTHER__ should remain" in out
    assert "__ENV_NOT_SUBSTITUTED__" in out


def test_render_is_idempotent_for_identical_inputs(tmp_path: Path) -> None:
    template = _make_template(tmp_path)
    service = _make_service(template)

    out1 = render_plist(service, install_dir=Path("/opt/amg"), home=Path("/Users/op"))
    out2 = render_plist(service, install_dir=Path("/opt/amg"), home=Path("/Users/op"))

    # Byte-identical second render — substitution has no hidden state.
    assert out1 == out2


def test_render_missing_template_raises_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.plist"
    service = _make_service(missing)

    with pytest.raises(FileNotFoundError) as exc_info:
        render_plist(service, install_dir=tmp_path, home=tmp_path)

    # The error message includes the canonical CLI-facing format so
    # callers can pass it straight through to typer.echo on stderr.
    assert "Plist template not found:" in str(exc_info.value)
    assert str(missing) in str(exc_info.value)


def test_render_substitution_handles_paths_with_special_chars(tmp_path: Path) -> None:
    # Paths can legitimately contain characters that would be regex
    # metacharacters (``.``, ``+``, ``$``, etc.). ``str.replace`` is
    # literal so this must just work.
    template = _make_template(tmp_path)
    service = _make_service(template)

    out = render_plist(
        service,
        install_dir=Path("/Users/foo+bar/$HOME/.amg"),
        home=Path("/Users/foo+bar"),
    )

    assert "/Users/foo+bar/$HOME/.amg" in out
    assert "__INSTALL_DIR__" not in out


# ---------------------------------------------------------------------------
# lint_plist — success / failure paths
# ---------------------------------------------------------------------------


def test_lint_invokes_plutil_lint_and_returns_tmpfile_on_success(
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []
    with patch(
        "amg.cli.plist.subprocess.run",
        side_effect=_fake_run_factory(captured, returncode=0),
    ):
        path = lint_plist("<plist/>")

    # Exactly one ``plutil -lint <tmpfile>`` invocation.
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[:2] == ["plutil", "-lint"]
    # The tmpfile path passed to plutil is the same path returned.
    assert cmd[2] == str(path)
    # Temp file exists, has a .plist suffix, and contains the text.
    assert path.exists()
    assert path.suffix == ".plist"
    assert path.read_text(encoding="utf-8") == "<plist/>"

    # Cleanup so subsequent tests don't accumulate /var/folders entries.
    path.unlink()


def test_lint_failure_raises_plist_lint_error_and_keeps_tmpfile(
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []
    with (
        patch(
            "amg.cli.plist.subprocess.run",
            side_effect=_fake_run_factory(captured, returncode=1, stderr="boom"),
        ),
        pytest.raises(PlistLintError) as exc_info,
    ):
        lint_plist("<invalid/>")

    # Temp file path is exposed via the exception so the caller can
    # surface it in the operator-facing error message.
    tmpfile = exc_info.value.tmpfile
    assert isinstance(tmpfile, Path)
    # Spec contract: file is retained for operator inspection.
    assert tmpfile.exists()
    assert tmpfile.read_text(encoding="utf-8") == "<invalid/>"
    # Error message matches the canonical CLI-facing format.
    assert str(tmpfile) in str(exc_info.value)
    assert "Rendered plist failed plutil -lint:" in str(exc_info.value)

    # Cleanup (test-only — production code does NOT delete on failure).
    tmpfile.unlink()


# ---------------------------------------------------------------------------
# install_plist — atomic replace, parent dir creation, lint plumbing
# ---------------------------------------------------------------------------


def test_install_writes_dest_atomically(tmp_path: Path) -> None:
    dest = tmp_path / "LaunchAgents" / "com.user.amg-test.plist"
    captured: list[list[str]] = []

    with patch(
        "amg.cli.plist.subprocess.run",
        side_effect=_fake_run_factory(captured, returncode=0),
    ):
        install_plist("<plist><contents/></plist>", dest)

    # ``plutil -lint`` ran before the file landed at dest.
    assert any(cmd[:2] == ["plutil", "-lint"] for cmd in captured)

    # Final contents at ``dest`` are exactly what we asked for. No partial
    # write window can produce different bytes because os.replace is
    # atomic within a filesystem.
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "<plist><contents/></plist>"


def test_install_creates_missing_parent_directory(tmp_path: Path) -> None:
    # Simulate the spec'd edge case: ``~/Library/LaunchAgents/`` does not
    # yet exist. install_plist must create it.
    parent = tmp_path / "Library" / "LaunchAgents"
    assert not parent.exists()
    dest = parent / "com.user.amg-test.plist"

    captured: list[list[str]] = []
    with patch(
        "amg.cli.plist.subprocess.run",
        side_effect=_fake_run_factory(captured, returncode=0),
    ):
        install_plist("<plist/>", dest)

    assert parent.is_dir()
    # Mode 0o700 — conservative default for ~/Library/-resident dirs.
    mode = stat.S_IMODE(parent.stat().st_mode)
    assert mode == 0o700


def test_install_propagates_lint_failure_without_writing_dest(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "LaunchAgents" / "com.user.amg-test.plist"

    captured: list[list[str]] = []
    with (
        patch(
            "amg.cli.plist.subprocess.run",
            side_effect=_fake_run_factory(captured, returncode=1),
        ),
        pytest.raises(PlistLintError) as exc_info,
    ):
        install_plist("<bad/>", dest)

    # The retained lint temp-file path is surfaced for the operator.
    assert exc_info.value.tmpfile.exists()
    # ``dest`` was never written — caller can re-run after editing the
    # template without worrying about a half-installed plist.
    assert not dest.exists()
    # Cleanup the retained temp file.
    exc_info.value.tmpfile.unlink()


def test_install_idempotent_overwrites_existing_dest(tmp_path: Path) -> None:
    dest = tmp_path / "LaunchAgents" / "com.user.amg-test.plist"
    dest.parent.mkdir(parents=True)
    dest.write_text("OLD CONTENTS", encoding="utf-8")

    captured: list[list[str]] = []
    with patch(
        "amg.cli.plist.subprocess.run",
        side_effect=_fake_run_factory(captured, returncode=0),
    ):
        install_plist("<plist>NEW</plist>", dest)

    assert dest.read_text(encoding="utf-8") == "<plist>NEW</plist>"


def test_install_no_temp_file_leak_in_dest_parent_on_success(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "LaunchAgents" / "com.user.amg-test.plist"

    captured: list[list[str]] = []
    with patch(
        "amg.cli.plist.subprocess.run",
        side_effect=_fake_run_factory(captured, returncode=0),
    ):
        install_plist("<plist/>", dest)

    # After a successful install, the only file in ``dest.parent`` is
    # ``dest`` itself — no leftover ``.tmp`` or dotfile temp files.
    siblings = list(dest.parent.iterdir())
    assert siblings == [dest]
