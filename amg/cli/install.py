"""Implementation of ``amg install`` (spec §5.3).

Pipeline (per target service):

    1. Render the service's plist template via :func:`amg.cli.plist.render_plist`,
       substituting ``__INSTALL_DIR__`` (repo root) and ``__HOME__``.
    2. Run ``plutil -lint`` over the rendered text and atomically replace
       ``~/Library/LaunchAgents/<label>.plist`` via
       :func:`amg.cli.plist.install_plist`.
    3. If the service is already bootstrapped (detected via
       :func:`amg.cli.launchctl.print_service`), ``launchctl bootout`` it
       first so the new plist takes effect.
    4. ``launchctl enable gui/<uid>/<label>`` then ``launchctl bootstrap
       gui/<uid> <plist>``. Enable precedes bootstrap because a persistent
       ``launchctl disable`` override (left behind by a prior ``amg service
       disable``) makes ``bootstrap`` fail with ``5: Input/output error``
       until the override is cleared.

Exit codes (spec §5.3 AC):

* ``0`` — every target succeeded.
* ``2`` — any non-zero ``launchctl`` exit, or ``plutil -lint`` rejected the
  rendered plist, or a plist template was missing.
* ``3`` — ``~/Library/LaunchAgents/`` is present but not writable by the
  current user. Surfaced before any launchctl work happens.

Logging side effects:

* Creates ``~/Library/Logs/messaging-agent/`` if missing (the launchd
  plists' ``StandardOutPath`` targets live there).
* Creates ``~/Library/LaunchAgents/`` if missing (mode ``0o700`` is set by
  :func:`amg.cli.plist.install_plist`).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from amg.cli.launchctl import bootout, bootstrap, enable, print_service
from amg.cli.output import print_status_line
from amg.cli.plist import PlistLintError, install_plist, render_plist
from amg.cli.services import Service, UnknownServiceError, resolve_targets

__all__ = ["run_install"]


# Repo root: ``amg/cli/install.py`` lives two parents up from the package
# root. Computed at import time; the CLI is single-process so no caching
# concerns apply. Mirrors the pattern in ``amg/api/healthz.py``.
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _launch_agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def _logs_dir(home: Path) -> Path:
    return home / "Library" / "Logs" / "messaging-agent"


def _ensure_logs_dir(home: Path) -> None:
    """Create ``~/Library/Logs/messaging-agent/`` if missing.

    Best-effort: failures here are non-fatal because launchd will create
    the file itself when the plist is loaded — but creating the directory
    up front means the first launch does not race with permissions.
    """
    _logs_dir(home).mkdir(parents=True, exist_ok=True)


def _check_launch_agents_writable(home: Path) -> int | None:
    """Return exit code 3 if LaunchAgents/ exists and is not writable.

    Returns ``None`` when the directory is either writable or absent.
    :func:`amg.cli.plist.install_plist` will create the directory itself if
    missing, so the "absent" case is not a failure here.
    """
    dest_dir = _launch_agents_dir(home)
    if dest_dir.exists() and not os.access(dest_dir, os.W_OK):
        return 3
    return None


# ``launchctl bootout`` is asynchronous: it returns as soon as the teardown
# is *requested*, not when it completes. Bootstrapping the same label again
# before launchd has finished removing it loses the race and fails with the
# generic ``5: Input/output error``. We poll ``print_service`` until the
# label is gone from the domain, bounded so a genuinely stuck teardown can't
# hang ``amg install`` forever. ~2 s total covers observed teardown latency
# with comfortable margin; this only runs on the re-install (already-loaded)
# path, never on a fresh install.
_BOOTOUT_SETTLE_ATTEMPTS: int = 10
_BOOTOUT_SETTLE_DELAY: float = 0.2


def _settle_after_bootout(label: str, *, sleep: Callable[[float], object] = time.sleep) -> None:
    """Best-effort wait until ``label`` is no longer loaded in the domain.

    Polls :func:`amg.cli.launchctl.print_service` up to
    :data:`_BOOTOUT_SETTLE_ATTEMPTS` times, sleeping
    :data:`_BOOTOUT_SETTLE_DELAY` seconds between checks. Returns as soon as
    the service reports ``loaded=False``. If the bound is exhausted it
    returns anyway and lets the subsequent ``bootstrap`` make the final call
    — surfacing launchctl's own error is better than blocking indefinitely.

    ``sleep`` is injectable so tests can run the loop without real delays.
    """
    for attempt in range(_BOOTOUT_SETTLE_ATTEMPTS):
        if not print_service(label).loaded:
            return
        # Don't sleep after the last check — nothing follows it.
        if attempt < _BOOTOUT_SETTLE_ATTEMPTS - 1:
            sleep(_BOOTOUT_SETTLE_DELAY)


def _install_one(
    service: Service,
    *,
    home: Path,
    install_dir: Path,
    err: IO[str],
) -> int:
    """Install one service. Returns ``0`` on success, ``2`` on any failure.

    Failure messages are emitted to ``err``; the per-service status line is
    printed by the caller so we always emit a one-line summary regardless
    of which step failed.
    """
    dest = _launch_agents_dir(home) / f"{service.label}.plist"
    install_dir_abs = install_dir.resolve()

    # --- Render -----------------------------------------------------------
    try:
        # Render against the repo-root-joined template path. The registry
        # stores repo-relative paths; joining here keeps the registry
        # portable across tests that override the install dir.
        template_abs = install_dir_abs / service.plist_template
        # Build a one-shot shim service with an absolute template path. We
        # avoid ``dataclasses.replace`` to keep imports light at module
        # scope; passing the absolute path through render_plist's
        # ``Path.read_text`` is sufficient because render_plist re-wraps
        # the path itself.
        rendered = render_plist(
            _with_template(service, template_abs),
            install_dir=install_dir_abs,
            home=home,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=err)
        return 2

    # --- Lint + atomic install -------------------------------------------
    try:
        install_plist(rendered, dest)
    except PlistLintError as exc:
        # Spec §5.3 Edge Cases: surface the tmpfile path so the operator
        # can re-run ``plutil -lint`` themselves.
        print(str(exc), file=err)
        return 2
    except OSError as exc:
        print(f"Failed to write {dest}: {exc}", file=err)
        return 2

    # --- launchctl bootout → enable → bootstrap --------------------------
    # If the service is already bootstrapped, bootout first so the new
    # plist takes effect. ``print_service`` returns ``loaded=False`` when
    # launchctl could not find it, in which case bootout is unnecessary.
    state = print_service(service.label)
    if state.loaded:
        result = bootout(service.label)
        if not result.ok:
            print(
                f"launchctl bootout failed for {service.label}: "
                f"{result.stderr.strip() or result.returncode}",
                file=err,
            )
            return 2
        # bootout is async — wait for the teardown to actually complete
        # before bootstrapping the same label, or the bootstrap races and
        # fails with ``5: Input/output error``.
        _settle_after_bootout(service.label)

    # ``enable`` MUST precede ``bootstrap``. ``launchctl disable`` writes a
    # persistent override into launchd's disabled database
    # (``/var/db/com.apple.xpc.launchd/disabled.<uid>.plist``) that survives
    # reboots, ``bootout``, and plist removal. While that override is set,
    # ``launchctl bootstrap`` refuses the service with the generic
    # ``5: Input/output error`` — so the override has to be cleared *first*.
    # ``enable`` is a safe no-op when the label was never disabled and works
    # even when the service is not currently loaded (it only touches the
    # disabled DB). See spec §5.3.
    result = enable(service.label)
    if not result.ok:
        print(
            f"launchctl enable failed for {service.label}: "
            f"{result.stderr.strip() or result.returncode}",
            file=err,
        )
        return 2

    result = bootstrap(service.label, str(dest))
    if not result.ok:
        print(
            f"launchctl bootstrap failed for {service.label}: "
            f"{result.stderr.strip() or result.returncode}",
            file=err,
        )
        return 2

    return 0


def _with_template(service: Service, template_abs: Path) -> Service:
    """Return a copy of ``service`` with an absolute template path.

    Used so :func:`amg.cli.plist.render_plist` can read the template
    without re-implementing repo-root joining. Implemented via
    :func:`dataclasses.replace` lazily so the import stays inside the
    function (keeps ``amg/cli/install.py`` import-light).
    """
    from dataclasses import replace

    return replace(service, plist_template=template_abs)


def run_install(
    names: list[str],
    *,
    home: Path | None = None,
    install_dir: Path | None = None,
    out: IO[str] | None = None,
    err: IO[str] | None = None,
) -> int:
    """Install one or more AMG launchd services.

    Args:
        names: Service identifiers from the command line. Empty list or
            ``["all"]`` means every registered service.
        home: Override ``$HOME``. Defaults to :func:`pathlib.Path.home`.
            Tests pass ``tmp_path`` here.
        install_dir: Override the repo root. Defaults to the on-disk repo
            root resolved at module import time. Tests pass ``tmp_path``.
        out: Stream for per-service result lines. Defaults to
            ``sys.stdout``.
        err: Stream for failure diagnostics. Defaults to ``sys.stderr``.

    Returns:
        Aggregate exit code (max of all per-service exit codes), where
        ``0 = all ok``, ``2 = some launchctl/lint failure``, ``3 =
        ~/Library/LaunchAgents/ not writable``.
    """
    home = home if home is not None else Path.home()
    install_dir = install_dir if install_dir is not None else _REPO_ROOT
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    # Resolve targets. Default to ["all"] when no names are supplied so the
    # spec'd "amg install" with no args fans out to every service.
    try:
        services = resolve_targets(names or ["all"])
    except UnknownServiceError as exc:
        print(str(exc), file=err)
        return 1

    # Writability check happens before the logs-dir mkdir so a perms
    # failure on LaunchAgents/ is not masked by an unrelated mkdir error.
    perms_code = _check_launch_agents_writable(home)
    if perms_code is not None:
        print(
            f"~/Library/LaunchAgents/ is not writable: {_launch_agents_dir(home)}",
            file=err,
        )
        return perms_code

    # Create the logs dir up front so launchd's first run does not race
    # with an empty filesystem. Best-effort; permission failures here
    # propagate as OSError but those are extremely unlikely for a
    # user-owned ~/Library/Logs/ subdir.
    _ensure_logs_dir(home)

    aggregate = 0
    for service in services:
        rc = _install_one(
            service,
            home=home,
            install_dir=install_dir,
            err=err,
        )
        if rc == 0:
            print_status_line(service.name, "installed", ok=True, stream=out)
        else:
            print_status_line(service.name, "failed", ok=False, stream=out)
        if rc > aggregate:
            aggregate = rc

    return aggregate
