"""Implementation of ``amg uninstall`` (spec §5.4).

This module is import-light by design — the top-level ``amg/cli/app.py``
defers importing it until the ``uninstall`` command actually runs, which
preserves the sub-200 ms cold-start budget (spec §6.1).

Per-target pipeline (spec §5.4 AC):

1. ``launchctl bootout gui/<uid>/<label>`` — tolerates the
   "not bootstrapped" case (exit 3 or 113) which :func:`amg.cli.launchctl.bootout`
   already maps to ``ok=True``.
2. Unless ``--keep-plist`` is passed, remove
   ``~/Library/LaunchAgents/<label>.plist``. A missing plist is treated as
   a warning, not a failure.

The aggregate exit code is the **maximum** of the per-target codes so that
"multiple targets, one fails" still produces a meaningful exit code while
the CLI continues across the rest (spec §5.4 Edge Cases).

Exit codes (spec §5.4):
    * 0 — success (or only no-op / missing-plist warnings).
    * 2 — unexpected ``launchctl`` failure on any target.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import IO

from amg.cli import launchctl
from amg.cli.output import print_status_line
from amg.cli.services import Service

__all__ = ["LAUNCH_AGENTS_DIR", "run_uninstall", "uninstall_one"]


# The user-LaunchAgents directory where ``amg install`` writes plists.
# Resolved at module-import time via :func:`Path.expanduser` so tests can
# monkeypatch ``HOME`` before importing the module, or override the
# directory via the ``launch_agents_dir`` argument on the run functions.
LAUNCH_AGENTS_DIR: Path = Path("~/Library/LaunchAgents").expanduser()


def _plist_path(service: Service, launch_agents_dir: Path) -> Path:
    """Return the on-disk plist path for *service* under *launch_agents_dir*."""
    return launch_agents_dir / f"{service.label}.plist"


def uninstall_one(
    service: Service,
    *,
    keep_plist: bool = False,
    launch_agents_dir: Path | None = None,
    stream: IO[str] | None = None,
) -> int:
    """Uninstall a single service: bootout + optional plist removal.

    Returns the per-target exit code:
        * 0 — bootout succeeded (or no-op-success) and plist removal (if
          requested) completed (or warned on a missing plist).
        * 2 — unexpected launchctl failure.

    A one-line status report is written to *stream* (or ``sys.stdout``)
    via :func:`print_status_line`.
    """
    target_dir = launch_agents_dir if launch_agents_dir is not None else LAUNCH_AGENTS_DIR

    boot = launchctl.bootout(service.label)
    if not boot.ok:
        # Unexpected launchctl failure. Surface stderr verbatim in the
        # detail field so the operator sees launchctl's own message.
        detail = (boot.stderr or "").strip() or f"launchctl exit {boot.returncode}"
        print_status_line(
            service.name,
            "bootout failed",
            detail=detail,
            ok=False,
            stream=stream,
        )
        return 2

    # bootout succeeded (or was a no-op because the service was already
    # unloaded). Build a status detail that distinguishes the two paths so
    # the operator can tell what actually happened.
    # No-op success: launchctl returned 3 or 113 ("not bootstrapped").
    bootout_detail = "unloaded" if boot.returncode == 0 else "already unloaded"

    if keep_plist:
        print_status_line(
            service.name,
            "uninstalled",
            detail=f"{bootout_detail}; plist preserved",
            ok=True,
            stream=stream,
        )
        return 0

    plist_path = _plist_path(service, target_dir)
    if not plist_path.exists():
        # Missing plist is a warning, not a failure (spec §5.4 Edge Cases).
        print_status_line(
            service.name,
            "uninstalled",
            detail=f"{bootout_detail}; plist already missing",
            ok=True,
            stream=stream,
        )
        return 0

    try:
        plist_path.unlink()
    except OSError as exc:
        # Filesystem failure deleting the plist. Treat as unexpected
        # failure with exit 2 so the operator notices.
        print_status_line(
            service.name,
            "plist removal failed",
            detail=str(exc),
            ok=False,
            stream=stream,
        )
        return 2

    print_status_line(
        service.name,
        "uninstalled",
        detail=f"{bootout_detail}; plist removed",
        ok=True,
        stream=stream,
    )
    return 0


def run_uninstall(
    services: Iterable[Service],
    *,
    keep_plist: bool = False,
    launch_agents_dir: Path | None = None,
    stream: IO[str] | None = None,
) -> int:
    """Run :func:`uninstall_one` across every service and aggregate exit codes.

    Continues across all targets even if one fails (spec §5.4 Edge Cases).
    The returned exit code is the **maximum** (worst) per-target code, so
    a mixed batch of OK + failed targets exits non-zero.
    """
    worst = 0
    for service in services:
        code = uninstall_one(
            service,
            keep_plist=keep_plist,
            launch_agents_dir=launch_agents_dir,
            stream=stream,
        )
        if code > worst:
            worst = code
    return worst
