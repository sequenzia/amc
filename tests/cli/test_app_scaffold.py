"""Tests for the top-level Typer CLI scaffold.

Spec references:
* §5.9 Top-Level UX — ``amg --help`` lists every command; ``amg service
  --help`` lists every lifecycle subcommand; ``amg --version`` prints the
  package version.
* §9.1 Phase 1 — these acceptance criteria gate the Foundation phase.

Coverage:
* ``--help`` mentions every top-level command (Functional).
* ``amg service --help`` mentions every service subcommand (Functional).
* ``--version`` returns the installed package version (Functional).
* Bare ``amg`` (no args) prints help and exits zero (Edge Case).
* Unknown command exits non-zero with a Typer-style error (Error Handling).
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version

from typer.testing import CliRunner

from amg.cli.app import app

runner = CliRunner()

TOP_LEVEL_COMMANDS = ("serve", "install", "uninstall", "status", "logs", "doctor", "service")
SERVICE_SUBCOMMANDS = ("start", "stop", "restart", "enable", "disable")


def test_help_lists_every_top_level_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for cmd in TOP_LEVEL_COMMANDS:
        assert cmd in result.output, f"missing {cmd!r} in --help output:\n{result.output}"


def test_service_help_lists_every_subcommand() -> None:
    result = runner.invoke(app, ["service", "--help"])
    assert result.exit_code == 0, result.output
    for cmd in SERVICE_SUBCOMMANDS:
        assert cmd in result.output, f"missing {cmd!r} in service --help output:\n{result.output}"


def test_version_prints_package_version() -> None:
    expected = pkg_version("amg")
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert expected in result.output


def test_no_args_prints_help() -> None:
    # Typer's ``no_args_is_help=True`` makes the bare invocation behave like
    # ``--help``. Typer signals this with exit code 0 (some versions use 2);
    # both are acceptable as long as the help surface is shown.
    result = runner.invoke(app, [])
    assert result.exit_code in (0, 2), result.output
    # Help text should mention at least one top-level command so we know
    # the operator got something useful, not a blank line.
    assert any(cmd in result.output for cmd in TOP_LEVEL_COMMANDS), result.output


def test_unknown_command_errors() -> None:
    result = runner.invoke(app, ["nope-not-a-real-command"])
    assert result.exit_code != 0


def test_help_exposes_shell_completion_flags() -> None:
    # Spec §12 Open Question #2: enable Typer's built-in shell completion.
    # ``--install-completion`` and ``--show-completion`` are added by Typer
    # automatically when ``add_completion=True`` is set on the top-level app.
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "--install-completion" in result.output
    assert "--show-completion" in result.output
