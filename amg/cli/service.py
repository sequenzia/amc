"""``amg service {start,stop,restart,enable,disable}`` (spec §5.5).

This module implements the per-service lifecycle verbs. Each verb resolves
the requested target list (``all`` by default), invokes the corresponding
``launchctl`` helper from :mod:`amg.cli.launchctl`, prints a per-service
result line via :func:`amg.cli.output.print_status_line`, and returns the
**aggregate** exit code — the worst (max) per-target exit code (spec §5.5
AC).

Per-verb exit-code contract:

* ``0`` — operation succeeded for this target.
* ``1`` — unknown service name (raised by
  :func:`amg.cli.services.resolve_targets`; caught at the command-body
  level before this module's per-target loop runs).
* ``2`` — launchctl reported a non-zero exit, **or** ``start`` requested a
  service whose plist file is missing on disk.

The module is import-light: ``launchctl``, ``output``, and ``services`` are
imported lazily inside command bodies so ``amg --help`` cold-starts under
200 ms (spec §6.1).

Spec references: §5.1 service registry, §5.5 lifecycle verbs, §6.1 startup
budget, §7.1 architecture.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from amg.cli import launchctl
from amg.cli.output import print_status_line
from amg.cli.services import Service

__all__ = [
    "LAUNCH_AGENTS_DIR",
    "run_start",
    "run_stop",
    "run_restart",
    "run_enable",
    "run_disable",
]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Directory under which installed launchd plist files live. ``amg install``
# writes ``<label>.plist`` here; ``amg service start`` reads it to attempt
# an auto-bootstrap when the service is not yet loaded (spec §5.5 Edge
# Cases). Module-level so tests can monkeypatch a temp dir.
LAUNCH_AGENTS_DIR: Path = Path("~/Library/LaunchAgents").expanduser()


def _plist_path(service: Service) -> Path:
    """Return the installed plist path for ``service`` (``<label>.plist``)."""
    return LAUNCH_AGENTS_DIR / f"{service.label}.plist"


# ---------------------------------------------------------------------------
# Per-target result helpers
# ---------------------------------------------------------------------------


def _emit(service_name: str, status: str, *, ok: bool, detail: str | None = None) -> None:
    """Print one result line via the shared status-line helper."""
    print_status_line(service_name, status, detail=detail, ok=ok)


def _exit_from_result(result: launchctl.CompletedResult) -> int:
    """Map a :class:`launchctl.CompletedResult` to a per-target exit code.

    Helpers that use the generic policy (``enable``, ``disable``,
    ``restart``) call this directly. ``start`` and ``stop`` apply their
    own edge-case handling first.
    """
    return 0 if result.ok else 2


# ---------------------------------------------------------------------------
# Verb implementations
# ---------------------------------------------------------------------------
#
# Each ``_do_*`` returns the per-target exit code (0 / 2). The driver
# :func:`_run_verb` walks the resolved target list, calls the right
# per-target function, prints a result line, and returns the aggregate
# (max) exit code.


def _do_start(service: Service) -> int:
    """``amg service start`` for one service.

    Tries ``launchctl kickstart`` first. If launchctl reports the service
    is not loaded — detected indirectly by checking
    :func:`launchctl.print_service` for ``loaded=False`` *after* a failed
    kickstart — and the installed plist file exists, attempt to
    ``bootstrap`` it then retry the kickstart. If the plist is missing,
    print the install-first message and return exit code 2 (spec §5.5
    Edge Cases).

    The "is it loaded?" check is done via ``print_service`` (and not by
    parsing kickstart's stderr) because launchctl's error wording for
    "service not bootstrapped" has shifted between macOS versions; the
    ``print`` returncode is the stable signal.
    """
    # Fast path: if it's already loaded, kickstart succeeds and we're done.
    first = launchctl.kickstart(service.label)
    if first.ok:
        _emit(service.name, "started", ok=True)
        return 0

    # Not loaded? Try to auto-bootstrap from the installed plist (spec
    # §5.5). We re-check via print_service rather than parsing kickstart
    # stderr because the wording varies across macOS versions.
    state = launchctl.print_service(service.label)
    if state.loaded:
        # Loaded but kickstart failed for some other reason — surface it.
        _emit(
            service.name,
            "failed",
            ok=False,
            detail=(first.stderr or "kickstart failed").strip() or "kickstart failed",
        )
        return _exit_from_result(first)

    # Service is not loaded. Require an installed plist.
    plist = _plist_path(service)
    if not plist.exists():
        _emit(
            service.name,
            "failed",
            ok=False,
            detail="Service not installed; run amg install first",
        )
        return 2

    # Bootstrap and retry kickstart.
    bootstrap_result = launchctl.bootstrap(service.label, str(plist))
    if not bootstrap_result.ok:
        _emit(
            service.name,
            "failed",
            ok=False,
            detail=(bootstrap_result.stderr or "bootstrap failed").strip() or "bootstrap failed",
        )
        return 2

    retry = launchctl.kickstart(service.label)
    if retry.ok:
        _emit(service.name, "started", ok=True)
        return 0

    _emit(
        service.name,
        "failed",
        ok=False,
        detail=(retry.stderr or "kickstart failed").strip() or "kickstart failed",
    )
    return _exit_from_result(retry)


# launchctl stderr fragments that indicate the service is not running when
# ``kill SIGTERM`` is issued. Matching is case-insensitive substring; we
# accept several historical wordings because Apple has shifted them.
_ALREADY_STOPPED_FRAGMENTS: tuple[str, ...] = (
    "could not find service",
    "no such process",
    "process not running",
    "not running",
)


def _is_already_stopped(result: launchctl.CompletedResult) -> bool:
    """Heuristic: did ``launchctl kill`` fail because nothing was running?

    POSIX ESRCH (exit 3) and launchd's "could not find service in domain"
    (exit 113) are both "already stopped" — we treat them as success
    (spec §5.5 Edge Cases). Anything else, we look at stderr text.
    """
    if result.returncode in {3, 113}:
        return True
    stderr_lower = (result.stderr or "").lower()
    return any(frag in stderr_lower for frag in _ALREADY_STOPPED_FRAGMENTS)


def _do_stop(service: Service) -> int:
    """``amg service stop`` for one service.

    Maps to ``launchctl kill SIGTERM``. When the service is already
    stopped, exit 0 and print ``<name>: already stopped`` (spec §5.5
    Edge Case).
    """
    result = launchctl.kill_sigterm(service.label)
    if result.ok:
        _emit(service.name, "stopped", ok=True)
        return 0

    if _is_already_stopped(result):
        _emit(service.name, "already stopped", ok=True)
        return 0

    _emit(
        service.name,
        "failed",
        ok=False,
        detail=(result.stderr or "kill failed").strip() or "kill failed",
    )
    return 2


def _do_restart(service: Service) -> int:
    """``amg service restart`` — ``launchctl kickstart -k`` (spec §5.5)."""
    result = launchctl.kickstart(service.label, restart=True)
    if result.ok:
        _emit(service.name, "restarted", ok=True)
        return 0
    _emit(
        service.name,
        "failed",
        ok=False,
        detail=(result.stderr or "kickstart -k failed").strip() or "kickstart -k failed",
    )
    return _exit_from_result(result)


def _do_enable(service: Service) -> int:
    """``amg service enable`` — ``launchctl enable`` (spec §5.5)."""
    result = launchctl.enable(service.label)
    if result.ok:
        _emit(service.name, "enabled", ok=True)
        return 0
    _emit(
        service.name,
        "failed",
        ok=False,
        detail=(result.stderr or "enable failed").strip() or "enable failed",
    )
    return _exit_from_result(result)


def _do_disable(service: Service) -> int:
    """``amg service disable`` — ``launchctl disable`` (spec §5.5)."""
    result = launchctl.disable(service.label)
    if result.ok:
        _emit(service.name, "disabled", ok=True)
        return 0
    _emit(
        service.name,
        "failed",
        ok=False,
        detail=(result.stderr or "disable failed").strip() or "disable failed",
    )
    return _exit_from_result(result)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _run_verb(
    target: str | None,
    per_target: Callable[[Service], int],
) -> int:
    """Resolve ``target`` and invoke ``per_target`` for each service.

    Returns the **max** of per-target exit codes (spec §5.5 AC). On
    :class:`amg.cli.services.UnknownServiceError`, prints the error to
    stderr and returns exit code 1 (the registry's canonical exit code,
    spec §5.1).
    """
    # Import lazily so the unknown-service error path stays inside this
    # function rather than at module import — keeps the cold-start budget.
    from amg.cli.services import UnknownServiceError, resolve_targets

    name = target or "all"
    try:
        services = resolve_targets([name])
    except UnknownServiceError as exc:
        # Print to stderr in the canonical wording (the exception message
        # already matches spec §5.1).
        import sys

        sys.stderr.write(f"{exc}\n")
        return 1

    worst = 0
    for svc in services:
        code = per_target(svc)
        if code > worst:
            worst = code
    return worst


def run_start(target: str | None) -> int:
    """Driver for ``amg service start``."""
    return _run_verb(target, _do_start)


def run_stop(target: str | None) -> int:
    """Driver for ``amg service stop``."""
    return _run_verb(target, _do_stop)


def run_restart(target: str | None) -> int:
    """Driver for ``amg service restart``."""
    return _run_verb(target, _do_restart)


def run_enable(target: str | None) -> int:
    """Driver for ``amg service enable``."""
    return _run_verb(target, _do_enable)


def run_disable(target: str | None) -> int:
    """Driver for ``amg service disable``."""
    return _run_verb(target, _do_disable)
