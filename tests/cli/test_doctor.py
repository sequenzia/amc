"""Tests for ``amg doctor`` (spec §5.8).

Coverage:
* Each individual check function returns the documented status for each
  fixture state (Functional).
* The FDA / chat.db check returns ``warn`` (not ``fail``) when the file is
  unreadable, with the launchd-principal explanatory message (Edge Case).
* A check that raises produces a synthetic ``fail`` row with
  ``"<ExceptionClass>: <msg>"`` and doctor keeps running (Edge Case +
  Error Handling).
* ``run_checks`` collects status in order.
* ``run_doctor(json_mode=True)`` emits JSON-parseable output with the
  required field shape (Functional).
* Exit-code matrix: all ok/warn → 0; any fail → 2 (Functional).
* The ``amg doctor`` Typer command surfaces the exit code (Integration).

Strategy:
* Patch module attributes (``CHAT_DB_PATH``, ``ENV_PATH``,
  ``LAUNCH_AGENTS_DIR``, ``LOG_DIR``, ``REPO_ROOT``, ``_default_run``)
  via ``monkeypatch.setattr`` to point at tmp_path fixtures, so the
  checks exercise real filesystem behavior without touching the real
  user environment.
* For ``check_uv_on_path`` we patch ``shutil.which`` at the doctor
  module's import-namespace via ``monkeypatch.setattr(doctor, "shutil", ...)``
  to keep the patch local.
* For port checks we inject ``probe`` directly.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from amg.cli import doctor
from amg.cli.app import app
from amg.cli.doctor import (
    check_chat_db_readable,
    check_env_file,
    check_log_dir_writable,
    check_plist_templates_exist,
    check_plists_installed,
    check_port_available,
    check_services_bootstrapped,
    check_uv_on_path,
    run_checks,
    run_doctor,
)
from amg.cli.services import REGISTRY

runner = CliRunner()


# ---------------------------------------------------------------------------
# Individual checks: uv on PATH
# ---------------------------------------------------------------------------


def test_check_uv_on_path_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/local/bin/uv")
    status, details = check_uv_on_path()
    assert status == "ok"
    assert "/usr/local/bin/uv" in details


def test_check_uv_on_path_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    status, details = check_uv_on_path()
    assert status == "fail"
    assert "uv" in details.lower()


# ---------------------------------------------------------------------------
# Individual checks: env file
# ---------------------------------------------------------------------------


def test_check_env_file_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("AMG_BEARER_TOKEN=abc\n")
    monkeypatch.setattr(doctor, "ENV_PATH", env)
    status, details = check_env_file()
    assert status == "ok"
    assert str(env) in details


def test_check_env_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / "does-not-exist.env"
    monkeypatch.setattr(doctor, "ENV_PATH", env)
    status, details = check_env_file()
    assert status == "fail"
    assert "missing" in details


def test_check_env_file_unreadable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("x=1\n")
    monkeypatch.setattr(doctor, "ENV_PATH", env)
    # Force os.access to report not-readable without actually chmod'ing
    # (running tests as root would make chmod ineffective).
    monkeypatch.setattr(doctor.os, "access", lambda p, mode: False)
    status, details = check_env_file()
    assert status == "fail"
    assert "not readable" in details


# ---------------------------------------------------------------------------
# Individual checks: chat.db / FDA
# ---------------------------------------------------------------------------


def test_check_chat_db_readable_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chat = tmp_path / "chat.db"
    chat.write_bytes(b"SQLite format 3\x00")
    monkeypatch.setattr(doctor, "CHAT_DB_PATH", chat)
    status, details = check_chat_db_readable()
    assert status == "ok"
    assert str(chat) in details


def test_check_chat_db_readable_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chat = tmp_path / "missing-chat.db"
    monkeypatch.setattr(doctor, "CHAT_DB_PATH", chat)
    status, details = check_chat_db_readable()
    assert status == "warn"
    assert "iMessage" in details or "not found" in details


def test_check_chat_db_readable_permission_error_is_warn_with_fda_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chat = tmp_path / "chat.db"
    chat.write_bytes(b"x")
    monkeypatch.setattr(doctor, "CHAT_DB_PATH", chat)

    real_open = doctor.open if hasattr(doctor, "open") else open

    def fake_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if str(path) == str(chat):
            raise PermissionError("Operation not permitted")
        return real_open(path, mode, *args, **kwargs)

    # Patch the builtin via the module's namespace; doctor imports open as
    # a builtin so we patch via builtins.
    monkeypatch.setattr("builtins.open", fake_open)
    status, details = check_chat_db_readable()
    assert status == "warn"
    # The details must surface the explanatory FDA principal message.
    assert "Full Disk Access" in details
    assert "launchd" in details
    assert "CLI" in details


# ---------------------------------------------------------------------------
# Individual checks: plist templates
# ---------------------------------------------------------------------------


def test_check_plist_templates_exist_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Create the directory structure expected by every Service in REGISTRY.
    for svc in REGISTRY:
        full = tmp_path / svc.plist_template
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("<?xml version='1.0'?>\n")
    monkeypatch.setattr(doctor, "REPO_ROOT", tmp_path)
    status, details = check_plist_templates_exist()
    assert status == "ok"


def test_check_plist_templates_exist_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Create only the adapter template; receiver/backup are missing.
    adapter = REGISTRY[0]
    (tmp_path / adapter.plist_template).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / adapter.plist_template).write_text("x")
    monkeypatch.setattr(doctor, "REPO_ROOT", tmp_path)
    status, details = check_plist_templates_exist()
    assert status == "fail"
    # Receiver + backup short names must appear in the details.
    assert "receiver" in details
    assert "backup" in details


# ---------------------------------------------------------------------------
# Individual checks: plists installed
# ---------------------------------------------------------------------------


def test_check_plists_installed_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    for svc in REGISTRY:
        (agents / f"{svc.label}.plist").write_text("x")
    monkeypatch.setattr(doctor, "LAUNCH_AGENTS_DIR", agents)
    status, details = check_plists_installed()
    assert status == "ok"


def test_check_plists_installed_dir_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "LaunchAgents"  # not created
    monkeypatch.setattr(doctor, "LAUNCH_AGENTS_DIR", agents)
    status, details = check_plists_installed()
    assert status == "fail"
    assert "does not exist" in details
    # Must not crash on a missing directory (acceptance criterion).


def test_check_plists_installed_partial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / f"{REGISTRY[0].label}.plist").write_text("x")  # only adapter
    monkeypatch.setattr(doctor, "LAUNCH_AGENTS_DIR", agents)
    status, details = check_plists_installed()
    assert status == "fail"
    assert REGISTRY[1].label in details  # receiver missing
    assert REGISTRY[2].label in details  # backup missing


# ---------------------------------------------------------------------------
# Individual checks: services bootstrapped
# ---------------------------------------------------------------------------


def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["launchctl", "print"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_check_services_bootstrapped_ok() -> None:
    captured: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        return _fake_proc(returncode=0)

    status, details = check_services_bootstrapped(run=fake_run)
    assert status == "ok"
    # One launchctl print call per service.
    assert len(captured) == len(REGISTRY)
    for argv in captured:
        assert argv[:2] == ["launchctl", "print"]


def test_check_services_bootstrapped_partial() -> None:
    # Adapter loaded, others not.
    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        target = argv[-1]
        return _fake_proc(returncode=0 if REGISTRY[0].label in target else 113)

    status, details = check_services_bootstrapped(run=fake_run)
    assert status == "fail"
    assert REGISTRY[1].label in details
    assert REGISTRY[2].label in details
    assert REGISTRY[0].label not in details


# ---------------------------------------------------------------------------
# Individual checks: ports
# ---------------------------------------------------------------------------


def test_check_port_available_free() -> None:
    status, details = check_port_available(8080, probe=lambda port: False)
    assert status == "ok"
    assert "8080" in details


def test_check_port_available_in_use() -> None:
    status, details = check_port_available(8090, probe=lambda port: True)
    assert status == "fail"
    assert "8090" in details
    assert "lsof" in details  # operator-facing hint


# ---------------------------------------------------------------------------
# Individual checks: log directory
# ---------------------------------------------------------------------------


def test_check_log_dir_writable_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "LOG_DIR", tmp_path)
    status, details = check_log_dir_writable()
    assert status == "ok"


def test_check_log_dir_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    nonexistent = tmp_path / "missing-logs"
    monkeypatch.setattr(doctor, "LOG_DIR", nonexistent)
    status, details = check_log_dir_writable()
    assert status == "fail"
    assert "missing" in details


def test_check_log_dir_not_writable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "LOG_DIR", tmp_path)
    monkeypatch.setattr(
        doctor.os,
        "access",
        lambda path, mode: False if str(path) == str(tmp_path) else os.access(path, mode),
    )
    status, details = check_log_dir_writable()
    assert status == "fail"
    assert "not writable" in details


# ---------------------------------------------------------------------------
# Runner: exception handling
# ---------------------------------------------------------------------------


def test_run_checks_isolates_exceptions() -> None:
    def good() -> tuple[str, str]:
        return ("ok", "fine")

    def boom() -> tuple[str, str]:
        raise RuntimeError("kaboom")

    def also_good() -> tuple[str, str]:
        return ("warn", "noted")

    results = run_checks(
        [
            ("first", good),
            ("middle", boom),
            ("last", also_good),
        ]
    )
    # Doctor must continue past the raising check.
    assert [r.check for r in results] == ["first", "middle", "last"]
    assert results[0].status == "ok"
    assert results[1].status == "fail"
    assert results[1].details == "RuntimeError: kaboom"
    assert results[2].status == "warn"


def test_run_checks_normalizes_unknown_status() -> None:
    def odd() -> tuple[str, str]:
        return ("ERROR", "uppercased")  # invalid status

    results = run_checks([("odd", odd)])
    # Unknown status strings are coerced to ``fail`` so they cannot slip
    # past exit-code computation or color rendering.
    assert results[0].status == "fail"
    assert results[0].details == "uppercased"


# ---------------------------------------------------------------------------
# Renderer + entry point
# ---------------------------------------------------------------------------


def test_run_doctor_json_emits_parseable_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_checks = (
        ("first", lambda: ("ok", "fine")),
        ("second", lambda: ("warn", "noted")),
    )
    buf = io.StringIO()
    code = run_doctor(json_mode=True, stream=buf, checks=fixture_checks)
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert isinstance(payload, list)
    assert payload == [
        {"check": "first", "status": "ok", "details": "fine"},
        {"check": "second", "status": "warn", "details": "noted"},
    ]


def test_run_doctor_json_includes_failing_check() -> None:
    fixture_checks = (
        ("good", lambda: ("ok", "yes")),
        ("bad", lambda: ("fail", "nope")),
    )
    buf = io.StringIO()
    code = run_doctor(json_mode=True, stream=buf, checks=fixture_checks)
    assert code == 2
    payload = json.loads(buf.getvalue())
    statuses = [row["status"] for row in payload]
    assert statuses == ["ok", "fail"]


def test_run_doctor_plain_text_table() -> None:
    fixture_checks = (
        ("uv", lambda: ("ok", "found")),
        ("ports", lambda: ("warn", "8090 busy")),
    )
    buf = io.StringIO()
    code = run_doctor(json_mode=False, stream=buf, checks=fixture_checks)
    assert code == 0
    out = buf.getvalue()
    # Plain renderer emits header + separator + rows; no ANSI escapes.
    assert "check" in out
    assert "status" in out
    assert "details" in out
    assert "ok" in out
    assert "warn" in out
    assert "\x1b[" not in out  # no ANSI escapes on a non-TTY StringIO


# ---------------------------------------------------------------------------
# Exit-code matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected_code"),
    [
        (["ok", "ok", "ok"], 0),
        (["ok", "warn"], 0),
        (["warn", "warn", "warn"], 0),
        (["ok", "fail"], 2),
        (["warn", "fail"], 2),
        (["fail"], 2),
    ],
)
def test_run_doctor_exit_code_matrix(statuses: list[str], expected_code: int) -> None:
    fixture_checks = tuple((f"c{i}", (lambda s=s: (s, "x"))) for i, s in enumerate(statuses))
    buf = io.StringIO()
    code = run_doctor(json_mode=True, stream=buf, checks=fixture_checks)
    assert code == expected_code


# ---------------------------------------------------------------------------
# Typer integration
# ---------------------------------------------------------------------------


def test_amg_doctor_command_runs_and_returns_an_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end via the Typer app.

    We patch ``run_doctor`` rather than every individual check; the
    individual checks are covered above. The Typer wrapper's only
    responsibility is forwarding ``--json`` and translating the return
    code into a ``typer.Exit``.
    """
    captured: dict[str, Any] = {}

    def fake_run_doctor(*, json_mode: bool = False, stream: Any = None) -> int:
        captured["json_mode"] = json_mode
        # Write something so the operator can see output was emitted.
        out = stream if stream is not None else None
        if out is not None:
            out.write("[]\n")
        return 0

    monkeypatch.setattr("amg.cli.doctor.run_doctor", fake_run_doctor)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert captured["json_mode"] is False


def test_amg_doctor_command_forwards_json_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_doctor(*, json_mode: bool = False, stream: Any = None) -> int:
        captured["json_mode"] = json_mode
        return 0

    monkeypatch.setattr("amg.cli.doctor.run_doctor", fake_run_doctor)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    assert captured["json_mode"] is True


def test_amg_doctor_command_propagates_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_doctor(*, json_mode: bool = False, stream: Any = None) -> int:
        return 2

    monkeypatch.setattr("amg.cli.doctor.run_doctor", fake_run_doctor)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 2
