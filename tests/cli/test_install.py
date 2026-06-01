"""Tests for ``amc install`` (spec §5.3).

Coverage:
* Functional: ``amc install`` installs all three services in registry order.
* Functional: ``amc install adapter receiver`` installs only those two.
* Functional: already-bootstrapped service is bootout'd before bootstrap.
* Functional: not-bootstrapped service is bootstrapped without a bootout call.
* Functional: idempotent re-run produces the same plist bytes and re-runs
  bootout → enable → bootstrap.
* Edge Case: ``~/Library/LaunchAgents/`` is created if missing.
* Edge Case: existing plist with unknown contents is overwritten.
* Edge Case: existing plist but service not bootstrapped → still installs.
* Error Handling: ``plutil -lint`` failure → exit 2; tmpfile retained;
  message includes its path.
* Error Handling: ``~/Library/LaunchAgents/`` not writable → exit 3 with a
  clear message.
* Error Handling: ``launchctl bootstrap`` non-zero → exit 2.
* Error Handling: ``launchctl enable`` non-zero → exit 2.
* Per-service result lines: ``<name>: installed`` on success, ``<name>:
  failed`` on failure.

We monkeypatch ``amc.cli.launchctl._run`` (the single subprocess seam in
the launchctl module) and ``amc.cli.plist.subprocess.run`` (the seam in
the plist module) so the tests never shell out.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from amc.cli.app import app
from amc.cli.install import run_install

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


_TEMPLATE_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>__LABEL__</string>
    <key>WorkingDirectory</key>
    <string>__INSTALL_DIR__</string>
    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/messaging-agent/launchd-stdout.log</string>
</dict>
</plist>
"""


def _seed_templates(install_dir: Path) -> None:
    """Materialize the plist templates the registry references.

    The service registry stores repo-relative paths like
    ``ops/launchd/com.user.amc-adapter.plist``. ``run_install`` joins those
    against ``install_dir`` and feeds the result to ``render_plist``. We
    seed all three templates under ``install_dir/ops/launchd/`` so the
    pipeline can read them.
    """
    target = install_dir / "ops" / "launchd"
    target.mkdir(parents=True, exist_ok=True)
    for label in (
        "com.user.amc-adapter",
        "com.user.amc-webhook-receiver",
        "com.user.amc-backup",
    ):
        # ``__LABEL__`` is intentionally left in the body — it is *not* a
        # documented substitution placeholder, but it lets us assert the
        # rendered output still distinguishes services on disk.
        (target / f"{label}.plist").write_text(
            _TEMPLATE_BODY.replace("__LABEL__", label),
            encoding="utf-8",
        )


class _LaunchctlRecorder:
    """Recording fake for ``amc.cli.launchctl._run``.

    Builds a scriptable response table keyed on argv shape. Records every
    call's argv in the order it was made so tests can assert sequencing
    (bootout → enable → bootstrap).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        # Default response: success, empty output. Per-key overrides win.
        self._responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
        self._returncode_by_verb: dict[str, int] = {
            "print": 113,  # default: not loaded
            "bootout": 0,
            "bootstrap": 0,
            "enable": 0,
        }

    def set_print_loaded(self, label: str, loaded: bool) -> None:
        """Make ``launchctl print gui/<uid>/<label>`` succeed (loaded) or fail."""
        from amc.cli.launchctl import DOMAIN

        argv = ("launchctl", "print", f"{DOMAIN}/{label}")
        rc = 0 if loaded else 113
        # Minimal stdout that the parser accepts but doesn't populate fields.
        stdout = "state = running\npid = 4242\n" if loaded else ""
        self._responses[argv] = subprocess.CompletedProcess(
            args=list(argv), returncode=rc, stdout=stdout, stderr=""
        )

    def set_verb_returncode(self, verb: str, rc: int, stderr: str = "") -> None:
        """Force ``launchctl <verb> …`` to return ``rc`` for any label."""
        self._returncode_by_verb[verb] = rc
        # We also stash the stderr so failure messages can be asserted.
        self._stderr_by_verb: dict[str, str]
        if not hasattr(self, "_stderr_by_verb"):
            self._stderr_by_verb = {}
        self._stderr_by_verb[verb] = stderr

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        argv_t = tuple(argv)
        self.calls.append(argv_t)
        if argv_t in self._responses:
            return self._responses[argv_t]
        # Fall back to per-verb defaults.
        verb = argv[1] if len(argv) > 1 else ""
        rc = self._returncode_by_verb.get(verb, 0)
        stderr = ""
        if hasattr(self, "_stderr_by_verb"):
            stderr = self._stderr_by_verb.get(verb, "")
        stdout = ""
        # ``print`` returning 0 needs *some* stdout so the parser sees a
        # loaded service when verb default is 0 (e.g. a test that wants
        # everything loaded). Empty stdout is fine for parsing — fields
        # default to ``"-"``.
        return subprocess.CompletedProcess(
            args=list(argv), returncode=rc, stdout=stdout, stderr=stderr
        )


def _fake_plutil_lint_ok(captured: list[list[str]]):
    """Return a fake ``subprocess.run`` that records argv and returns ok."""

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    return _fake_run


def _fake_plutil_lint_fail(captured: list[list[str]]):
    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="invalid xml")

    return _fake_run


# ---------------------------------------------------------------------------
# Functional
# ---------------------------------------------------------------------------


def test_install_all_three_services(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()

    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        out = _CaptureStream()
        err = _CaptureStream()
        code = run_install([], home=home, install_dir=install_dir, out=out, err=err)

    assert code == 0, err.getvalue()

    # All three plists landed at ~/Library/LaunchAgents/.
    launch_agents = home / "Library" / "LaunchAgents"
    expected_files = {
        "com.user.amc-adapter.plist",
        "com.user.amc-webhook-receiver.plist",
        "com.user.amc-backup.plist",
    }
    on_disk = {p.name for p in launch_agents.iterdir()}
    assert expected_files <= on_disk

    # Logs dir was created.
    assert (home / "Library" / "Logs" / "messaging-agent").is_dir()

    # Each service got a `<name>: installed` result line, in registry order.
    out_text = out.getvalue()
    assert "adapter: installed" in out_text
    assert "receiver: installed" in out_text
    assert "backup: installed" in out_text
    # Order matches registry order (adapter, receiver, backup).
    adapter_idx = out_text.index("adapter:")
    receiver_idx = out_text.index("receiver:")
    backup_idx = out_text.index("backup:")
    assert adapter_idx < receiver_idx < backup_idx


def test_install_subset_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()

    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        out = _CaptureStream()
        err = _CaptureStream()
        code = run_install(
            ["adapter", "receiver"], home=home, install_dir=install_dir, out=out, err=err
        )

    assert code == 0
    launch_agents = home / "Library" / "LaunchAgents"
    on_disk = {p.name for p in launch_agents.iterdir()}
    assert "com.user.amc-adapter.plist" in on_disk
    assert "com.user.amc-webhook-receiver.plist" in on_disk
    assert "com.user.amc-backup.plist" not in on_disk
    out_text = out.getvalue()
    assert "adapter: installed" in out_text
    assert "receiver: installed" in out_text
    assert "backup" not in out_text


def test_install_bootout_when_already_bootstrapped(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    fake_run = _LaunchctlRecorder()
    # Mark the adapter as already loaded so install_one bootouts first.
    fake_run.set_print_loaded("com.user.amc-adapter", True)

    captured: list[list[str]] = []
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
        # The async-bootout settle wait polls print_service; stub it out so
        # this test asserts the bare verb sequence (settle is covered by its
        # own dedicated test below).
        patch("amc.cli.install._settle_after_bootout"),
    ):
        code = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=_CaptureStream(),
        )

    assert code == 0

    # Filter to just the adapter's launchctl calls.
    adapter_calls = [c for c in fake_run.calls if "com.user.amc-adapter" in " ".join(c)]
    verbs = [c[1] for c in adapter_calls]
    # Expected order: print → bootout → enable → bootstrap.
    assert verbs == ["print", "bootout", "enable", "bootstrap"]


def test_install_skips_bootout_when_not_bootstrapped(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    fake_run = _LaunchctlRecorder()
    # Default: print returns 113 → not loaded → bootout is skipped.

    captured: list[list[str]] = []
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        code = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=_CaptureStream(),
        )

    assert code == 0
    adapter_calls = [c for c in fake_run.calls if "com.user.amc-adapter" in " ".join(c)]
    verbs = [c[1] for c in adapter_calls]
    # No bootout in the sequence — service wasn't loaded.
    assert verbs == ["print", "enable", "bootstrap"]


def test_install_idempotent_second_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    fake_run = _LaunchctlRecorder()
    captured: list[list[str]] = []

    # First run: nothing loaded.
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        code1 = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=_CaptureStream(),
        )
    assert code1 == 0
    dest = home / "Library" / "LaunchAgents" / "com.user.amc-adapter.plist"
    first_bytes = dest.read_bytes()

    # Second run: now mark as loaded; expect bootout this time.
    fake_run.set_print_loaded("com.user.amc-adapter", True)
    fake_run.calls.clear()
    captured.clear()

    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
        # Stub the async-bootout settle wait; it polls print_service and
        # would otherwise add poll calls to the recorded verb sequence.
        patch("amc.cli.install._settle_after_bootout"),
    ):
        code2 = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=_CaptureStream(),
        )
    assert code2 == 0
    second_bytes = dest.read_bytes()
    # Idempotent on disk: same bytes after re-render.
    assert first_bytes == second_bytes
    # Idempotent on launchctl: bootout → enable → bootstrap runs again.
    adapter_calls = [c for c in fake_run.calls if "com.user.amc-adapter" in " ".join(c)]
    verbs = [c[1] for c in adapter_calls]
    assert verbs == ["print", "bootout", "enable", "bootstrap"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_install_creates_launch_agents_dir_with_mode_700(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    launch_agents = home / "Library" / "LaunchAgents"
    assert not launch_agents.exists()

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        code = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=_CaptureStream(),
        )

    assert code == 0
    assert launch_agents.is_dir()
    mode = stat.S_IMODE(launch_agents.stat().st_mode)
    assert mode == 0o700


def test_install_overwrites_existing_plist_with_unknown_contents(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    dest = home / "Library" / "LaunchAgents" / "com.user.amc-adapter.plist"
    dest.parent.mkdir(parents=True)
    dest.write_text("LEGACY DO NOT KEEP", encoding="utf-8")

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        code = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=_CaptureStream(),
        )

    assert code == 0
    new_contents = dest.read_text(encoding="utf-8")
    assert "LEGACY" not in new_contents
    assert "com.user.amc-adapter" in new_contents


def test_install_existing_plist_but_service_not_bootstrapped(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    # Plist exists but launchctl print returns "not loaded" (default 113).
    dest = home / "Library" / "LaunchAgents" / "com.user.amc-adapter.plist"
    dest.parent.mkdir(parents=True)
    dest.write_text("OLD", encoding="utf-8")

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        code = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=_CaptureStream(),
        )

    assert code == 0
    # No bootout call — service wasn't loaded — but enable + bootstrap
    # still ran cleanly against the freshly-rewritten plist.
    adapter_calls = [c for c in fake_run.calls if "com.user.amc-adapter" in " ".join(c)]
    verbs = [c[1] for c in adapter_calls]
    assert verbs == ["print", "enable", "bootstrap"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_install_plutil_lint_failure_exits_two_keeps_tmpfile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_fail(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        err = _CaptureStream()
        code = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=err,
        )

    assert code == 2
    err_text = err.getvalue()
    assert "plutil -lint" in err_text
    # Extract the temp-file path from the error message and confirm it's
    # still on disk (the spec contract is "leave temp file in place").
    # The message format is ``Rendered plist failed plutil -lint: <path>``.
    last_line = err_text.strip().splitlines()[-1]
    tmp_path_str = last_line.rsplit(":", 1)[-1].strip()
    assert Path(tmp_path_str).exists()
    # Cleanup so we don't accumulate /var/folders entries across test runs.
    Path(tmp_path_str).unlink()


def test_install_launch_agents_not_writable_exits_three(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    # Create LaunchAgents/ and remove write permission. (We restore mode
    # afterwards so pytest's cleanup doesn't choke.)
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    original_mode = launch_agents.stat().st_mode
    launch_agents.chmod(0o500)  # r-x only
    try:
        captured: list[list[str]] = []
        fake_run = _LaunchctlRecorder()
        with (
            patch(
                "amc.cli.plist.subprocess.run",
                side_effect=_fake_plutil_lint_ok(captured),
            ),
            patch("amc.cli.launchctl._run", side_effect=fake_run),
        ):
            err = _CaptureStream()
            code = run_install(
                ["adapter"],
                home=home,
                install_dir=install_dir,
                out=_CaptureStream(),
                err=err,
            )
    finally:
        launch_agents.chmod(original_mode)

    assert code == 3
    err_text = err.getvalue()
    assert "not writable" in err_text
    # No subprocess work should have happened — perms guard fires first.
    assert captured == []
    assert fake_run.calls == []


@pytest.mark.parametrize("verb", ["bootstrap", "enable"])
def test_install_launchctl_failure_exits_two(verb: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    fake_run = _LaunchctlRecorder()
    fake_run.set_verb_returncode(verb, 5, stderr=f"{verb} blew up")
    captured: list[list[str]] = []

    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        out = _CaptureStream()
        err = _CaptureStream()
        code = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=out,
            err=err,
        )

    assert code == 2
    assert f"launchctl {verb}" in err.getvalue()
    assert "adapter: failed" in out.getvalue()


def test_install_bootout_failure_exits_two(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    fake_run = _LaunchctlRecorder()
    # Make it look loaded so we attempt bootout.
    fake_run.set_print_loaded("com.user.amc-adapter", True)
    # bootout returns a non-noop failure code (anything other than 0, 3, 113).
    fake_run.set_verb_returncode("bootout", 5, stderr="bootout died")
    captured: list[list[str]] = []

    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        out = _CaptureStream()
        err = _CaptureStream()
        code = run_install(
            ["adapter"],
            home=home,
            install_dir=install_dir,
            out=out,
            err=err,
        )

    assert code == 2
    assert "launchctl bootout" in err.getvalue()
    assert "adapter: failed" in out.getvalue()


# ---------------------------------------------------------------------------
# _settle_after_bootout — async-bootout race guard
# ---------------------------------------------------------------------------


def test_settle_after_bootout_polls_until_unloaded() -> None:
    """Returns as soon as ``print_service`` reports the label unloaded.

    ``launchctl bootout`` is async; the settle wait must keep polling while
    the service is still loaded and stop on the first ``loaded=False``,
    sleeping between checks but never after the final one.
    """
    from amc.cli import install as install_mod
    from amc.cli.launchctl import PrintResult

    # Loaded for the first two checks, then torn down.
    states = iter([True, True, False])
    counts = {"print": 0, "sleep": 0}

    def fake_print(label: str) -> PrintResult:
        counts["print"] += 1
        return PrintResult(label=label, loaded=next(states), returncode=0)

    def fake_sleep(_seconds: float) -> None:
        counts["sleep"] += 1

    with patch("amc.cli.install.print_service", side_effect=fake_print):
        install_mod._settle_after_bootout("com.user.amc-adapter", sleep=fake_sleep)

    # Stopped on the 3rd check (first unloaded); slept twice between checks.
    assert counts["print"] == 3
    assert counts["sleep"] == 2


def test_settle_after_bootout_is_bounded() -> None:
    """A teardown that never completes still terminates the loop.

    The wait is best-effort: when the bound is exhausted it returns and
    lets the subsequent ``bootstrap`` surface the real error rather than
    blocking ``amc install`` forever.
    """
    from amc.cli import install as install_mod
    from amc.cli.launchctl import PrintResult

    counts = {"print": 0, "sleep": 0}

    def always_loaded(label: str) -> PrintResult:
        counts["print"] += 1
        return PrintResult(label=label, loaded=True, returncode=0)

    def fake_sleep(_seconds: float) -> None:
        counts["sleep"] += 1

    with patch("amc.cli.install.print_service", side_effect=always_loaded):
        install_mod._settle_after_bootout("com.user.amc-adapter", sleep=fake_sleep)

    # Bounded: exactly _BOOTOUT_SETTLE_ATTEMPTS checks, one fewer sleep
    # (no sleep follows the final check).
    assert counts["print"] == install_mod._BOOTOUT_SETTLE_ATTEMPTS
    assert counts["sleep"] == install_mod._BOOTOUT_SETTLE_ATTEMPTS - 1


def test_install_mixed_targets_aggregate_max_code(tmp_path: Path) -> None:
    """One target fails, another succeeds → exit code is the max (2)."""
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    fake_run = _LaunchctlRecorder()
    # Fail bootstrap only for the receiver.
    from amc.cli.launchctl import DOMAIN

    receiver_bootstrap_argv = (
        "launchctl",
        "bootstrap",
        DOMAIN,
        str(home / "Library" / "LaunchAgents" / "com.user.amc-webhook-receiver.plist"),
    )
    fake_run._responses[receiver_bootstrap_argv] = subprocess.CompletedProcess(
        args=list(receiver_bootstrap_argv),
        returncode=5,
        stdout="",
        stderr="receiver bootstrap failed",
    )

    captured: list[list[str]] = []
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        out = _CaptureStream()
        err = _CaptureStream()
        code = run_install(
            ["adapter", "receiver"],
            home=home,
            install_dir=install_dir,
            out=out,
            err=err,
        )

    assert code == 2
    out_text = out.getvalue()
    assert "adapter: installed" in out_text
    assert "receiver: failed" in out_text


def test_install_unknown_service_exits_one(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        err = _CaptureStream()
        code = run_install(
            ["bogus"],
            home=home,
            install_dir=install_dir,
            out=_CaptureStream(),
            err=err,
        )
    assert code == 1
    assert "Unknown service" in err.getvalue()


# ---------------------------------------------------------------------------
# CLI wiring (CliRunner smoke test)
# ---------------------------------------------------------------------------


def test_cli_install_invokes_run_install(monkeypatch: Any, tmp_path: Path) -> None:
    """``amc install`` end-to-end via CliRunner: dispatches to run_install."""
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)

    # Redirect HOME so the CLI's default home path lands under tmp_path.
    monkeypatch.setenv("HOME", str(home))

    # Stash the install_dir at module import time so the real
    # _REPO_ROOT is overridden for this test.
    import amc.cli.install as install_mod

    monkeypatch.setattr(install_mod, "_REPO_ROOT", install_dir)

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        result = runner.invoke(app, ["install", "adapter"])

    assert result.exit_code == 0, f"{result.output}\nstderr={result.stderr}"
    assert "adapter: installed" in result.output


def test_cli_install_no_args_means_all(monkeypatch: Any, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "repo"
    install_dir.mkdir()
    _seed_templates(install_dir)
    monkeypatch.setenv("HOME", str(home))

    import amc.cli.install as install_mod

    monkeypatch.setattr(install_mod, "_REPO_ROOT", install_dir)

    captured: list[list[str]] = []
    fake_run = _LaunchctlRecorder()
    with (
        patch("amc.cli.plist.subprocess.run", side_effect=_fake_plutil_lint_ok(captured)),
        patch("amc.cli.launchctl._run", side_effect=fake_run),
    ):
        result = runner.invoke(app, ["install"])

    assert result.exit_code == 0, f"{result.output}\nstderr={result.stderr}"
    assert "adapter: installed" in result.output
    assert "receiver: installed" in result.output
    assert "backup: installed" in result.output


# ---------------------------------------------------------------------------
# Stream double
# ---------------------------------------------------------------------------


class _CaptureStream:
    """Minimal ``write``/``getvalue`` stream for ``run_install`` ``out``/``err``."""

    def __init__(self) -> None:
        self._buf: list[str] = []

    def write(self, text: str) -> int:
        self._buf.append(text)
        return len(text)

    def flush(self) -> None:  # pragma: no cover — required by some writers
        return None

    def isatty(self) -> bool:  # used by output helpers
        return False

    def getvalue(self) -> str:
        return "".join(self._buf)


# Silence the test-collection warning about unused import on POSIX-only os.
_ = os
