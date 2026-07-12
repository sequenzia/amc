"""Service registry for the ``amg`` CLI.

This module is the **single source of truth** for the launchd-managed AMG
services: ``adapter`` (FastAPI app on port 8080), ``receiver`` (webhook
receiver on port 8090), and ``backup`` (scheduled sqlite backup; no port).

Every CLI command — ``install``, ``uninstall``, ``status``, ``logs``,
``doctor``, and ``service start/stop/restart/enable/disable`` — resolves
its target list through :func:`resolve_targets`. This guarantees:

* The three services are listed in a stable order (``adapter`` first, then
  ``receiver``, then ``backup``).
* Operators may refer to a service by either its short name (``adapter``)
  or its full launchd label (``com.user.amg-adapter``).
* The keyword ``all`` (and the empty input) fan out to every service.
* Unknown names raise :class:`UnknownServiceError`, which the CLI maps to
  exit code 1 with the canonical error message from spec §5.1.

The module is intentionally import-light: no heavy stdlib modules beyond
``dataclasses`` and ``pathlib`` are touched, so importing it does not pay
into the 200 ms cold-start budget (spec §6.1).

Spec references: §5.1 Service Registry, §7.1 Architecture Overview.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Service",
    "UnknownServiceError",
    "REGISTRY",
    "resolve_targets",
]


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
# The registry stores **relative** paths for ``plist_template`` and
# ``run_script`` (relative to the repo root) so that the registry remains
# usable from tests that do not install plists on disk. Commands that need
# absolute paths join these against the repo root themselves.
#
# Log paths, by contrast, are absolute and ``~``-expanded because launchd
# writes to those paths verbatim and operators expect them to match what
# they see in the plist files on disk.
#
# Source-of-truth cross-check: the values below mirror the plist files
# committed under ``ops/launchd/``. If those plists change, this registry
# must change in lockstep. ``amg doctor`` (spec §5.8) verifies that the
# plist templates referenced here exist on disk.

_OPS_LAUNCHD = Path("ops/launchd")
_LOG_DIR = Path("~/Library/Logs/messaging-agent").expanduser()


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Service:
    """Metadata for one launchd-managed AMG service.

    Fields:
        name: Short, human-friendly identifier (``adapter``, ``receiver``,
            ``backup``). Used as the primary CLI argument.
        label: Full launchd label (``com.user.amg-adapter``). Used to
            construct the ``gui/<uid>/<label>`` service target for every
            ``launchctl`` invocation.
        plist_template: Repo-relative path to the plist template under
            ``ops/launchd/``. ``amg install`` reads this, performs
            ``__INSTALL_DIR__`` / ``__HOME__`` substitution, and writes the
            result to ``~/Library/LaunchAgents/<label>.plist``.
        run_script: Repo-relative path to the wrapper shell script that
            launchd invokes via ``ProgramArguments``. For ``backup`` this
            points at ``ops/scripts/backup.sh`` (not yet on disk in v1;
            tracked in spec §5.1 edge cases).
        app_log_glob: Glob pattern for the structured app-level log files
            under ``~/Library/Logs/messaging-agent/`` (e.g.
            ``adapter-*.log``). Used by ``amg logs`` to find rotated daily
            log files. ``None`` for services that do not emit app-level
            logs (none today, but the field shape allows it).
        launchd_stdout: Absolute path to the launchd-level stdout log file
            (the ``StandardOutPath`` from the plist).
        launchd_stderr: Absolute path to the launchd-level stderr log file
            (the ``StandardErrorPath`` from the plist).
        port: TCP port the service binds to (``8080`` for adapter,
            ``8090`` for receiver, ``None`` for backup which is a one-shot
            scheduled job).
    """

    name: str
    label: str
    plist_template: Path
    run_script: Path
    app_log_glob: str
    launchd_stdout: Path
    launchd_stderr: Path
    port: int | None


class UnknownServiceError(ValueError):
    """Raised when a service name or label is not in the registry.

    The CLI catches this in command bodies and maps it to exit code 1 with
    the message ``Unknown service: <name>. Known: adapter, receiver,
    backup, all.`` (spec §5.1, Edge Cases).
    """


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Order matters: ``resolve_targets(["all"])`` returns the registry in this
# order (spec §5.1). The CLI's own ``--help`` listings and ``amg status``
# output also rely on this ordering.
REGISTRY: tuple[Service, ...] = (
    Service(
        name="adapter",
        label="com.user.amg-adapter",
        plist_template=_OPS_LAUNCHD / "com.user.amg-adapter.plist",
        run_script=_OPS_LAUNCHD / "run-adapter.sh",
        app_log_glob="adapter-*.log",
        launchd_stdout=_LOG_DIR / "launchd-stdout.log",
        launchd_stderr=_LOG_DIR / "launchd-stderr.log",
        port=8080,
    ),
    Service(
        name="receiver",
        label="com.user.amg-webhook-receiver",
        plist_template=_OPS_LAUNCHD / "com.user.amg-webhook-receiver.plist",
        run_script=_OPS_LAUNCHD / "run-webhook-receiver.sh",
        app_log_glob="receiver-*.log",
        launchd_stdout=_LOG_DIR / "launchd-receiver-stdout.log",
        launchd_stderr=_LOG_DIR / "launchd-receiver-stderr.log",
        port=8090,
    ),
    Service(
        name="backup",
        label="com.user.amg-backup",
        plist_template=_OPS_LAUNCHD / "com.user.amg-backup.plist",
        # The backup run script lives at ops/scripts/backup.sh per the
        # backup plist's ProgramArguments. It is not yet on disk in v1;
        # ``amg doctor`` will surface its absence (spec §5.8).
        run_script=Path("ops/scripts/backup.sh"),
        app_log_glob="backup-*.log",
        launchd_stdout=_LOG_DIR / "launchd-backup-stdout.log",
        launchd_stderr=_LOG_DIR / "launchd-backup-stderr.log",
        port=None,
    ),
)


# Lookup indices. Built once at import time so ``resolve_targets`` is O(k)
# in the number of input names, not O(k·n).
_BY_NAME: dict[str, Service] = {s.name: s for s in REGISTRY}
_BY_LABEL: dict[str, Service] = {s.label: s for s in REGISTRY}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_targets(names: list[str]) -> list[Service]:
    """Resolve user-supplied service identifiers to :class:`Service` records.

    Behavior:
        * Empty input or ``["all"]`` returns every registered service in
          the stable order ``adapter, receiver, backup``.
        * Each name may be either a short name (``adapter``) or a full
          launchd label (``com.user.amg-adapter``).
        * Duplicates are deduplicated while preserving first-seen order.
          ``resolve_targets(["adapter", "adapter", "receiver"])`` returns
          ``[adapter, receiver]``.
        * Mixed inputs work: ``resolve_targets(["adapter",
          "com.user.amg-backup"])`` returns ``[adapter, backup]``.

    Args:
        names: List of identifiers. May be empty.

    Returns:
        A new list of :class:`Service` records.

    Raises:
        UnknownServiceError: If any name is neither a known short name nor
            a known label nor the ``all`` keyword. The error message is
            the canonical CLI message from spec §5.1.
    """
    # Empty list or the literal ["all"] expands to every service. We treat
    # ["all"] as the only valid use of the all keyword — mixing it with
    # other names would be ambiguous (does "adapter all" mean "all" or
    # "adapter plus all"?). The spec only commits to the singleton form,
    # so we keep this strict.
    if not names or names == ["all"]:
        return list(REGISTRY)

    resolved: list[Service] = []
    seen: set[str] = set()  # tracks labels (canonical) to dedupe across forms

    for name in names:
        service = _BY_NAME.get(name) or _BY_LABEL.get(name)
        if service is None:
            raise UnknownServiceError(
                f"Unknown service: {name}. Known: adapter, receiver, backup, all."
            )
        # Dedupe on canonical label so ("adapter", "com.user.amg-adapter")
        # collapses to a single entry. First-seen order is preserved.
        if service.label in seen:
            continue
        seen.add(service.label)
        resolved.append(service)

    return resolved
