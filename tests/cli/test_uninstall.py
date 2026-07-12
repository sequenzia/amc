"""Tests for ``amg uninstall`` (spec §5.4).

Coverage matrix:

Functional (spec §5.4 AC):
* ``amg uninstall`` (no args) removes all three services and their plists.
* ``amg uninstall adapter`` removes only the adapter.
* ``--keep-plist`` boots out the service but leaves the plist on disk.
* Already-unloaded service (launchctl exit 3 or 113) succeeds — no error.
* Exit 0 on success.

Edge Cases (spec §5.4):
* Plist already missing → status line warns, exit code stays 0.
* Multiple targets, one fails → continue with the rest; aggregate exit
  code is the worst (max) per-target code.

Error Handling (spec §5.4):
* Unexpected ``launchctl bootout`` failure → exit 2; stderr surfaced in
  the per-service status line detail.

Strategy:
* Patch ``amg.cli.launchctl._run`` to inject canned subprocess results.
  This is the lowest-level seam in the launchctl module and lets us
  control returncode/stderr per call.
* Use ``tmp_path`` as a fake ``~/Library/LaunchAgents`` directory and
  pass it through via the ``launch_agents_dir`` kwarg of the run
  functions. The CLI command itself reads
  ``amg.cli.uninstall.LAUNCH_AGENTS_DIR`` — we monkeypatch that constant
  for the CliRunner tests.
* ``CliRunner`` exercises the full Typer surface; direct calls to
  :func:`uninstall_one` / :func:`run_uninstall` exercise the unit-level
  behaviors without coupling to Typer.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from amg.cli import launchctl, uninstall
from amg.cli.app import app
from amg.cli.services import REGISTRY

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake launchctl._run
# ---------------------------------------------------------------------------


def _fake_run_factory(
    *,
    returncode_by_label: dict[str, int] | None = None,
    default_returncode: int = 0,
    stderr: str = "",
    captured: list[list[str]] | None = None,
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Return a stand-in for :func:`launchctl._run`.

    Looks up the returncode by the service label (argv index 2 is the
    full ``gui/<uid>/<label>`` target) so a single fake can serve a
    multi-target uninstall test with mixed outcomes.
    """
    rc_map = returncode_by_label or {}

    def _fake(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if captured is not None:
            captured.append(list(argv))
        # The bootout target is at argv[-1] in the form gui/<uid>/<label>.
        target = argv[-1]
        label = target.rsplit("/", 1)[-1]
        rc = rc_map.get(label, default_returncode)
        return subprocess.CompletedProcess(
            args=argv,
            returncode=rc,
            stdout="",
            stderr=stderr,
        )

    return _fake


# ---------------------------------------------------------------------------
# Helpers for plist file setup
# ---------------------------------------------------------------------------


def _seed_plists(launch_agents_dir: Path, *, include: set[str] | None = None) -> None:
    """Create empty plist files for each service label in *launch_agents_dir*.

    If *include* is provided, only those labels get a plist created.
    """
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    for svc in REGISTRY:
        if include is not None and svc.label not in include:
            continue
        (launch_agents_dir / f"{svc.label}.plist").write_text(
            "<?xml version='1.0' encoding='UTF-8'?>\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Functional: full sweep
# ---------------------------------------------------------------------------


def test_uninstall_default_removes_all_services_and_plists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``amg uninstall`` (no args) calls bootout for each label and deletes the plist."""
    monkeypatch.setattr(uninstall, "LAUNCH_AGENTS_DIR", tmp_path)
    _seed_plists(tmp_path)

    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _fake_run_factory(captured=captured))

    result = runner.invoke(app, ["uninstall"])

    assert result.exit_code == 0, result.output + result.stderr
    # Every service got a bootout invocation with the right target shape.
    assert len(captured) == len(REGISTRY)
    for argv, svc in zip(captured, REGISTRY, strict=True):
        assert argv[:2] == ["launchctl", "bootout"]
        assert argv[-1].endswith(f"/{svc.label}")
    # All plists were deleted.
    for svc in REGISTRY:
        assert not (tmp_path / f"{svc.label}.plist").exists(), (
            f"plist for {svc.name} should be removed"
        )
    # Each service appears in the status output.
    for svc in REGISTRY:
        assert svc.name in result.stdout


def test_uninstall_single_target_only_touches_that_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``amg uninstall adapter`` boots out only adapter; receiver/backup untouched."""
    monkeypatch.setattr(uninstall, "LAUNCH_AGENTS_DIR", tmp_path)
    _seed_plists(tmp_path)

    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _fake_run_factory(captured=captured))

    result = runner.invoke(app, ["uninstall", "adapter"])

    assert result.exit_code == 0, result.output + result.stderr
    assert len(captured) == 1
    assert captured[0][-1].endswith("/com.user.amg-adapter")
    # Adapter plist gone, others remain.
    assert not (tmp_path / "com.user.amg-adapter.plist").exists()
    assert (tmp_path / "com.user.amg-webhook-receiver.plist").exists()
    assert (tmp_path / "com.user.amg-backup.plist").exists()


# ---------------------------------------------------------------------------
# Functional: --keep-plist
# ---------------------------------------------------------------------------


def test_keep_plist_flag_preserves_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--keep-plist`` performs bootout but leaves the plist on disk."""
    monkeypatch.setattr(uninstall, "LAUNCH_AGENTS_DIR", tmp_path)
    _seed_plists(tmp_path)

    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _fake_run_factory(captured=captured))

    result = runner.invoke(app, ["uninstall", "adapter", "--keep-plist"])

    assert result.exit_code == 0, result.output + result.stderr
    # bootout was still called.
    assert len(captured) == 1
    assert captured[0][:2] == ["launchctl", "bootout"]
    # Plist remains.
    assert (tmp_path / "com.user.amg-adapter.plist").exists()
    assert "plist preserved" in result.stdout


# ---------------------------------------------------------------------------
# Functional: already-unloaded service is a success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rc", [3, 113])
def test_already_unloaded_is_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rc: int,
) -> None:
    """launchctl exit 3 or 113 ("not bootstrapped") is treated as no-op success."""
    monkeypatch.setattr(uninstall, "LAUNCH_AGENTS_DIR", tmp_path)
    _seed_plists(tmp_path, include={"com.user.amg-adapter"})

    monkeypatch.setattr(
        launchctl,
        "_run",
        _fake_run_factory(
            default_returncode=rc,
            stderr="Could not find service in domain",
        ),
    )

    result = runner.invoke(app, ["uninstall", "adapter"])

    assert result.exit_code == 0, result.output + result.stderr
    assert "already unloaded" in result.stdout
    # Plist still gets removed even on no-op bootout.
    assert not (tmp_path / "com.user.amg-adapter.plist").exists()


# ---------------------------------------------------------------------------
# Edge: plist already missing
# ---------------------------------------------------------------------------


def test_missing_plist_warns_but_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plist absent → status warns ("already missing"), exit 0."""
    monkeypatch.setattr(uninstall, "LAUNCH_AGENTS_DIR", tmp_path)
    # Do NOT seed a plist file.
    tmp_path.mkdir(exist_ok=True)

    monkeypatch.setattr(launchctl, "_run", _fake_run_factory())

    result = runner.invoke(app, ["uninstall", "adapter"])

    assert result.exit_code == 0, result.output + result.stderr
    assert "plist already missing" in result.stdout


# ---------------------------------------------------------------------------
# Edge: multiple targets, one fails → aggregate = worst
# ---------------------------------------------------------------------------


def test_aggregate_exit_code_is_max_across_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One target fails (unexpected exit) → continue, final code = max (2)."""
    monkeypatch.setattr(uninstall, "LAUNCH_AGENTS_DIR", tmp_path)
    _seed_plists(tmp_path)

    # adapter succeeds (0), receiver fails (1 — not in _BOOTOUT_NOOP_EXITS),
    # backup succeeds (0). Expected aggregate exit code: 2.
    captured: list[list[str]] = []
    monkeypatch.setattr(
        launchctl,
        "_run",
        _fake_run_factory(
            returncode_by_label={"com.user.amg-webhook-receiver": 1},
            stderr="boom",
            captured=captured,
        ),
    )

    result = runner.invoke(app, ["uninstall"])

    assert result.exit_code == 2, result.output + result.stderr
    # All three services were attempted (continue across failures).
    assert len(captured) == 3
    # Adapter and backup plists were removed; receiver plist remains because
    # bootout failed for it (we don't remove on failure path).
    assert not (tmp_path / "com.user.amg-adapter.plist").exists()
    assert (tmp_path / "com.user.amg-webhook-receiver.plist").exists()
    assert not (tmp_path / "com.user.amg-backup.plist").exists()
    # The failure detail is present in the per-service status line.
    assert "receiver" in result.stdout
    assert "bootout failed" in result.stdout


# ---------------------------------------------------------------------------
# Error handling: unexpected launchctl failure → exit 2 with stderr
# ---------------------------------------------------------------------------


def test_unexpected_launchctl_failure_exits_two_with_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Single-target unexpected failure (exit 1) → exit 2 and stderr surfaced."""
    monkeypatch.setattr(uninstall, "LAUNCH_AGENTS_DIR", tmp_path)
    _seed_plists(tmp_path, include={"com.user.amg-adapter"})

    monkeypatch.setattr(
        launchctl,
        "_run",
        _fake_run_factory(
            default_returncode=1,
            stderr="permission denied",
        ),
    )

    result = runner.invoke(app, ["uninstall", "adapter"])

    assert result.exit_code == 2, result.output + result.stderr
    assert "bootout failed" in result.stdout
    assert "permission denied" in result.stdout
    # Plist must NOT be removed on bootout failure.
    assert (tmp_path / "com.user.amg-adapter.plist").exists()


# ---------------------------------------------------------------------------
# Unit-level: run_uninstall returns aggregate
# ---------------------------------------------------------------------------


def test_run_uninstall_returns_zero_on_all_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_plists(tmp_path)
    monkeypatch.setattr(launchctl, "_run", _fake_run_factory())

    stream = io.StringIO()
    code = uninstall.run_uninstall(
        list(REGISTRY),
        launch_agents_dir=tmp_path,
        stream=stream,
    )
    assert code == 0
    # Every service line appears in the captured stream.
    for svc in REGISTRY:
        assert svc.name in stream.getvalue()


def test_run_uninstall_returns_max_across_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_plists(tmp_path)
    # First target fails (1), others succeed (0). Expected aggregate: 2.
    monkeypatch.setattr(
        launchctl,
        "_run",
        _fake_run_factory(
            returncode_by_label={"com.user.amg-adapter": 1},
            stderr="boom",
        ),
    )

    code = uninstall.run_uninstall(
        list(REGISTRY),
        launch_agents_dir=tmp_path,
        stream=io.StringIO(),
    )
    assert code == 2


def test_uninstall_one_keep_plist_skips_unlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct ``uninstall_one`` call with ``keep_plist=True`` leaves plist alone."""
    _seed_plists(tmp_path, include={"com.user.amg-adapter"})
    monkeypatch.setattr(launchctl, "_run", _fake_run_factory())

    adapter = next(s for s in REGISTRY if s.name == "adapter")
    plist = tmp_path / f"{adapter.label}.plist"
    assert plist.exists()

    code = uninstall.uninstall_one(
        adapter,
        keep_plist=True,
        launch_agents_dir=tmp_path,
        stream=io.StringIO(),
    )
    assert code == 0
    assert plist.exists(), "keep_plist=True must not remove the plist"


def test_uninstall_one_already_unloaded_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct ``uninstall_one`` with launchctl exit 113 returns 0."""
    _seed_plists(tmp_path, include={"com.user.amg-adapter"})
    monkeypatch.setattr(
        launchctl,
        "_run",
        _fake_run_factory(default_returncode=113),
    )

    adapter = next(s for s in REGISTRY if s.name == "adapter")
    code = uninstall.uninstall_one(
        adapter,
        launch_agents_dir=tmp_path,
        stream=io.StringIO(),
    )
    assert code == 0


def test_unknown_service_name_returns_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unknown service short name surfaces as exit 1 (resolve_targets path)."""
    monkeypatch.setattr(uninstall, "LAUNCH_AGENTS_DIR", tmp_path)

    # No launchctl call should happen — assert by failing if _run is called.
    def _explode(argv: list[str]) -> Any:
        raise AssertionError(f"launchctl should not be invoked: {argv}")

    monkeypatch.setattr(launchctl, "_run", _explode)

    result = runner.invoke(app, ["uninstall", "bogus"])
    assert result.exit_code == 1
    assert "Unknown service" in result.stderr
