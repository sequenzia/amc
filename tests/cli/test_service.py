"""Tests for ``amg service {start,stop,restart,enable,disable}`` (spec §5.5).

Coverage matrix:

* **Functional** — each verb invokes the exact ``launchctl`` argv documented
  in spec §5.5 (``kickstart``, ``kill SIGTERM``, ``kickstart -k``,
  ``enable``, ``disable``); default target ``all`` iterates the three
  services in registry order.
* **Edge cases** —
    - ``stop`` on an already-stopped service exits 0 with the canonical
      ``<name>: already stopped`` message.
    - ``start`` on a service whose plist file is missing exits 2 with the
      install-first message.
    - ``start`` auto-bootstraps when the service is installed but not
      loaded, then retries kickstart.
* **Error handling** —
    - Unknown service name exits 1.
    - Mixed success/failure across targets → aggregate exit code is the
      worst (max) of the per-target codes (spec §5.5 AC).

Strategy:

* Patch the launchctl helpers' single subprocess seam (``_run``) via a
  table keyed by the leading argv tokens. This lets one test exercise the
  whole verb (multiple launchctl invocations per service for the
  auto-bootstrap path) without needing to stub each helper individually.
* For tests that need an installed plist, monkeypatch
  :data:`amg.cli.service.LAUNCH_AGENTS_DIR` to a ``tmp_path`` and
  ``touch`` the expected ``<label>.plist``.
* ``CliRunner`` is used so both the Typer wiring and the underlying
  driver are exercised end-to-end.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from amg.cli import launchctl, service
from amg.cli.app import app
from amg.cli.services import REGISTRY

runner = CliRunner()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _target(label: str) -> str:
    """The exact ``gui/<uid>/<label>`` string used by every launchctl call."""
    return f"gui/{os.getuid()}/{label}"


def _install_fake_run(
    monkeypatch: Any,
    *,
    responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
    default: subprocess.CompletedProcess[str] | None = None,
    captured: list[list[str]] | None = None,
) -> list[list[str]]:
    """Patch :func:`launchctl._run` with a table-driven fake.

    ``responses`` is keyed by a **prefix tuple** of argv tokens; the longest
    matching prefix wins. ``default`` is used when nothing matches. If
    ``captured`` is provided, every argv invocation is appended to it.
    """
    captured = [] if captured is None else captured
    resp_table: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = responses or {}

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        # Find the longest-prefix match.
        best: subprocess.CompletedProcess[str] | None = None
        best_len = -1
        for key, value in resp_table.items():
            if len(key) <= len(argv) and tuple(argv[: len(key)]) == key and len(key) > best_len:
                best = value
                best_len = len(key)
        if best is not None:
            return best
        if default is not None:
            return default
        # Sensible default: success with empty output.
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launchctl, "_run", fake_run)
    return captured


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail(returncode: int = 1, stderr: str = "boom") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _service_names() -> list[str]:
    return [s.name for s in REGISTRY]


# ---------------------------------------------------------------------------
# Functional: exact argv per verb
# ---------------------------------------------------------------------------


def test_start_single_service_invokes_kickstart_argv(monkeypatch: Any) -> None:
    """``amg service start adapter`` runs ``launchctl kickstart gui/<uid>/<label>``."""
    captured = _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "start", "adapter"])
    assert result.exit_code == 0, result.output
    assert captured == [["launchctl", "kickstart", _target("com.user.amg-adapter")]]
    assert "adapter" in result.stdout
    assert "started" in result.stdout


def test_stop_single_service_invokes_kill_sigterm_argv(monkeypatch: Any) -> None:
    captured = _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "stop", "adapter"])
    assert result.exit_code == 0
    assert captured == [["launchctl", "kill", "SIGTERM", _target("com.user.amg-adapter")]]
    assert "stopped" in result.stdout


def test_restart_uses_dash_k_form(monkeypatch: Any) -> None:
    """Spec §5.5: restart uses ``kickstart -k``."""
    captured = _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "restart", "adapter"])
    assert result.exit_code == 0
    assert captured == [["launchctl", "kickstart", "-k", _target("com.user.amg-adapter")]]
    assert "restarted" in result.stdout


def test_enable_invokes_enable_argv(monkeypatch: Any) -> None:
    captured = _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "enable", "adapter"])
    assert result.exit_code == 0
    assert captured == [["launchctl", "enable", _target("com.user.amg-adapter")]]
    assert "enabled" in result.stdout


def test_disable_invokes_disable_argv(monkeypatch: Any) -> None:
    captured = _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "disable", "adapter"])
    assert result.exit_code == 0
    assert captured == [["launchctl", "disable", _target("com.user.amg-adapter")]]
    assert "disabled" in result.stdout


# ---------------------------------------------------------------------------
# Functional: default target & all-mode iterates registry order
# ---------------------------------------------------------------------------


def test_start_default_target_is_all(monkeypatch: Any) -> None:
    """Omitting the target name fans out across every registered service."""
    captured = _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "start"])
    assert result.exit_code == 0
    # One kickstart per registered service, in registry order.
    labels = [svc.label for svc in REGISTRY]
    assert captured == [["launchctl", "kickstart", _target(label)] for label in labels]


def test_enable_all_iterates_registry_order(monkeypatch: Any) -> None:
    captured = _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "enable", "all"])
    assert result.exit_code == 0
    labels = [svc.label for svc in REGISTRY]
    assert captured == [["launchctl", "enable", _target(label)] for label in labels]
    # Per-service result line for every service, in registry order.
    out = result.stdout
    for name in _service_names():
        assert name in out


# ---------------------------------------------------------------------------
# Edge case: stop on already-stopped service
# ---------------------------------------------------------------------------


def test_stop_already_stopped_exits_zero(monkeypatch: Any) -> None:
    """``kill SIGTERM`` returning ESRCH (3) yields exit 0 + ``already stopped`` message."""
    _install_fake_run(
        monkeypatch,
        default=_fail(returncode=3, stderr="kill: 1: No such process"),
    )
    result = runner.invoke(app, ["service", "stop", "adapter"])
    assert result.exit_code == 0
    assert "adapter: already stopped" in result.stdout


def test_stop_already_stopped_via_stderr_text(monkeypatch: Any) -> None:
    """launchctl returning a non-standard exit but a recognized stderr fragment."""
    _install_fake_run(
        monkeypatch,
        default=_fail(returncode=1, stderr="Could not find service in domain"),
    )
    result = runner.invoke(app, ["service", "stop", "receiver"])
    assert result.exit_code == 0
    assert "receiver: already stopped" in result.stdout


# ---------------------------------------------------------------------------
# Edge case: start on service whose plist is missing
# ---------------------------------------------------------------------------


def test_start_missing_plist_exits_two_with_install_first_message(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Service not bootstrapped + plist missing → exit 2 with canonical message."""
    # Empty LaunchAgents dir — no plist file present for any service.
    monkeypatch.setattr(service, "LAUNCH_AGENTS_DIR", tmp_path)

    # First call: kickstart fails (not loaded). Then print_service: also
    # fails (loaded=False). No bootstrap should be attempted because the
    # plist is missing.
    captured = _install_fake_run(
        monkeypatch,
        responses={
            ("launchctl", "kickstart"): _fail(returncode=113, stderr="not loaded"),
            ("launchctl", "print"): _fail(returncode=113, stderr="not loaded"),
        },
    )
    result = runner.invoke(app, ["service", "start", "adapter"])
    assert result.exit_code == 2
    assert "Service not installed; run amg install first" in result.stdout
    # No bootstrap should have been attempted.
    assert not any(argv[:2] == ["launchctl", "bootstrap"] for argv in captured)


# ---------------------------------------------------------------------------
# Edge case: auto-bootstrap on start
# ---------------------------------------------------------------------------


def test_start_auto_bootstraps_when_installed_but_not_loaded(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """First kickstart fails, print_service reports not-loaded, plist exists →
    bootstrap is attempted and kickstart is retried.
    """
    # Point LaunchAgents at a tmp dir and create the adapter plist file.
    monkeypatch.setattr(service, "LAUNCH_AGENTS_DIR", tmp_path)
    adapter = REGISTRY[0]
    plist_path = tmp_path / f"{adapter.label}.plist"
    plist_path.write_text("<plist/>")

    # Track call order so we can assert the auto-bootstrap retry sequence.
    captured: list[list[str]] = []

    # First kickstart call fails, then bootstrap succeeds, then retry
    # kickstart succeeds. We swap _run dynamically based on call count
    # because the test wants different responses for two identical argv
    # vectors (the two kickstart invocations).
    kickstart_calls = {"count": 0}

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        if argv[:2] == ["launchctl", "kickstart"]:
            kickstart_calls["count"] += 1
            if kickstart_calls["count"] == 1:
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=113,
                    stdout="",
                    stderr="not bootstrapped",
                )
            return _ok()
        if argv[:2] == ["launchctl", "print"]:
            # Not loaded — drives the bootstrap branch.
            return subprocess.CompletedProcess(args=argv, returncode=113, stdout="", stderr="")
        if argv[:2] == ["launchctl", "bootstrap"]:
            return _ok()
        return _ok()

    monkeypatch.setattr(launchctl, "_run", fake_run)

    result = runner.invoke(app, ["service", "start", "adapter"])
    assert result.exit_code == 0, result.output

    # Expected sequence: kickstart (fail) → print (not loaded) → bootstrap
    # (ok) → kickstart (ok).
    kinds = [argv[1] for argv in captured]
    assert kinds == ["kickstart", "print", "bootstrap", "kickstart"]
    # The bootstrap argv must include the plist path we created.
    bootstrap_argv = [a for a in captured if a[1] == "bootstrap"][0]
    assert str(plist_path) in bootstrap_argv
    assert "started" in result.stdout


# ---------------------------------------------------------------------------
# Error handling: unknown service name → exit 1
# ---------------------------------------------------------------------------


def test_unknown_service_name_exits_one(monkeypatch: Any) -> None:
    captured = _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "start", "nope"])
    assert result.exit_code == 1
    # Canonical error message from spec §5.1.
    assert "Unknown service: nope" in result.stderr
    # No launchctl call should have been attempted.
    assert captured == []


# ---------------------------------------------------------------------------
# Error handling: aggregate exit code = max of per-target codes
# ---------------------------------------------------------------------------


def test_all_mode_aggregate_exit_code_reflects_worst(monkeypatch: Any) -> None:
    """Mixed success/failure across targets → aggregate exit is the worst."""
    # First service (adapter) succeeds; second (receiver) fails; third
    # (backup) succeeds. Aggregate exit code must be 2.
    receiver_label = REGISTRY[1].label

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1] == "enable" and receiver_label in argv[-1]:
            return _fail(returncode=1, stderr="permission denied")
        return _ok()

    monkeypatch.setattr(launchctl, "_run", fake_run)

    result = runner.invoke(app, ["service", "enable", "all"])
    assert result.exit_code == 2
    # The failing target's result line is "failed"; the others succeeded.
    out = result.stdout
    assert "receiver: failed" in out
    assert "adapter: enabled" in out
    assert "backup: enabled" in out


def test_all_mode_all_success_returns_zero(monkeypatch: Any) -> None:
    _install_fake_run(monkeypatch, default=_ok())
    result = runner.invoke(app, ["service", "disable", "all"])
    assert result.exit_code == 0
    for name in _service_names():
        assert f"{name}: disabled" in result.stdout
