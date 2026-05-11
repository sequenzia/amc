"""Tests for ``amc serve`` (spec §5.2).

Coverage:
* ``amc serve adapter`` execs the documented uv argv vector (Functional).
* ``amc serve receiver`` execs the receiver argv with ``--project
  webhook-receiver`` (Functional).
* ``--host`` / ``--port`` overrides are reflected in the execvp argv
  (Functional).
* ``amc serve backup`` exits 1 with a clear message and never calls
  ``os.execvp`` (Edge Case).
* ``uv`` not on PATH → ``FileNotFoundError`` from ``os.execvp`` is mapped
  to exit 2 with a clear message (Error Handling).
* Unknown target exits non-zero without calling ``os.execvp``.

The tests monkeypatch ``os.execvp`` so the test process is not replaced.
"""

from __future__ import annotations

import os
from typing import Any

from typer.testing import CliRunner

from amc.cli.app import app

runner = CliRunner()


class _ExecvpCalled(Exception):
    """Sentinel raised by the monkeypatched ``os.execvp`` to short-circuit.

    Carries the captured argv so tests can assert on it. We use an
    exception (rather than letting the patched stub return normally)
    because the production code treats successful ``execvp`` as
    "process replaced" — control should not return.
    """

    def __init__(self, file: str, argv: list[str]) -> None:
        super().__init__(file)
        self.file = file
        self.argv = argv


def _install_execvp_spy(monkeypatch: Any) -> list[_ExecvpCalled]:
    """Patch ``os.execvp`` to record calls without replacing the process."""
    calls: list[_ExecvpCalled] = []

    def fake_execvp(file: str, argv: list[str]) -> None:
        record = _ExecvpCalled(file, list(argv))
        calls.append(record)
        raise record

    monkeypatch.setattr(os, "execvp", fake_execvp)
    return calls


def test_serve_adapter_execs_default_argv(monkeypatch: Any) -> None:
    calls = _install_execvp_spy(monkeypatch)
    result = runner.invoke(app, ["serve", "adapter"])
    # The fake execvp raises an exception; Typer surfaces it as a non-zero
    # exit and stashes the exception on the result. We don't care about the
    # exit code here — we care about the captured argv.
    assert calls, f"os.execvp was not called: {result.output}\nstderr={result.stderr}"
    assert len(calls) == 1
    assert calls[0].file == "uv"
    assert calls[0].argv == [
        "uv",
        "run",
        "uvicorn",
        "amc.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8080",
    ]


def test_serve_receiver_execs_default_argv(monkeypatch: Any) -> None:
    calls = _install_execvp_spy(monkeypatch)
    runner.invoke(app, ["serve", "receiver"])
    assert calls, "os.execvp was not called"
    assert calls[0].file == "uv"
    assert calls[0].argv == [
        "uv",
        "run",
        "--project",
        "webhook-receiver",
        "uvicorn",
        "amc_receiver.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8090",
    ]


def test_serve_adapter_host_and_port_overrides(monkeypatch: Any) -> None:
    calls = _install_execvp_spy(monkeypatch)
    runner.invoke(app, ["serve", "adapter", "--host", "0.0.0.0", "--port", "9000"])
    assert calls, "os.execvp was not called"
    assert calls[0].argv == [
        "uv",
        "run",
        "uvicorn",
        "amc.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "9000",
    ]


def test_serve_receiver_host_and_port_overrides(monkeypatch: Any) -> None:
    calls = _install_execvp_spy(monkeypatch)
    runner.invoke(app, ["serve", "receiver", "--host", "0.0.0.0", "--port", "9100"])
    assert calls, "os.execvp was not called"
    assert calls[0].argv == [
        "uv",
        "run",
        "--project",
        "webhook-receiver",
        "uvicorn",
        "amc_receiver.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "9100",
    ]


def test_serve_backup_exits_one_without_execvp(monkeypatch: Any) -> None:
    calls = _install_execvp_spy(monkeypatch)
    result = runner.invoke(app, ["serve", "backup"])
    assert calls == [], "os.execvp must not be called for backup"
    assert result.exit_code == 1
    assert "backup runs only under launchd" in result.stderr
    assert "amc service start backup" in result.stderr


def test_serve_uv_not_found_exits_two(monkeypatch: Any) -> None:
    def fake_execvp(file: str, argv: list[str]) -> None:
        raise FileNotFoundError(2, "No such file or directory", file)

    monkeypatch.setattr(os, "execvp", fake_execvp)
    result = runner.invoke(app, ["serve", "adapter"])
    assert result.exit_code == 2
    assert "uv not found on PATH" in result.stderr


def test_serve_unknown_target_exits_nonzero(monkeypatch: Any) -> None:
    calls = _install_execvp_spy(monkeypatch)
    result = runner.invoke(app, ["serve", "bogus"])
    assert calls == [], "os.execvp must not be called for unknown target"
    assert result.exit_code != 0
