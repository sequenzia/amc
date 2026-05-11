"""``amc status`` — show launchd state for AMC services (spec §5.6).

For each target service, this module:

1. Calls :func:`amc.cli.launchctl.print_service` to get parsed
   ``launchctl print gui/<uid>/<label>`` output (loaded flag, pid,
   last exit, state, start_time).
2. Calls :func:`amc.cli.launchctl.print_disabled_state` **once** to
   build a ``{label: enabled}`` mapping covering every service in a
   single ``launchctl print-disabled`` invocation.
3. Computes a best-effort ``uptime`` string: when the service is
   loaded, has a numeric PID, and ``ps -o lstart`` returns a parseable
   start time, that delta against ``now`` is humanized to
   ``"3m 12s"`` / ``"2h 8m"`` / ``"4d"``. Falls back to ``"-"`` when
   any prerequisite fails.
4. Builds a row dict per service (``service``, ``loaded``, ``enabled``,
   ``pid``, ``last_exit``, ``uptime``) and delegates rendering to
   :func:`amc.cli.output.render_table` so the Rich / plain / JSON
   format-selection logic stays in one place.

Exit policy (spec §5.6 AC):

* ``0`` — every queried service is **loaded** (running or not).
* ``1`` — unknown service name (raised as :class:`UnknownServiceError`
  from the registry; the command body maps to exit 1 with the
  canonical "Known: …" message).
* ``2`` — any queried service is **not loaded** (regardless of how
  many were queried). A "loaded but not running" service is still
  considered informational and does not bump the exit code.

The module is import-light so ``amc --help`` does not pay for any of
this (spec §6.1) — the command body in :mod:`amc.cli.app` lazy-imports
:func:`run_status`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from typing import IO

from amc.cli.launchctl import (
    PrintResult,
    print_disabled_state,
    print_service,
)
from amc.cli.services import (
    REGISTRY,
    Service,
    UnknownServiceError,
    resolve_targets,
)

__all__ = [
    "run_status",
    "build_row",
    "humanize_uptime",
    "probe_process_start",
]


# ---------------------------------------------------------------------------
# Subprocess seam for ``ps -o lstart``
# ---------------------------------------------------------------------------


def _run_ps(pid: str) -> subprocess.CompletedProcess[str]:
    """Run ``ps -o lstart= -p <pid>`` and capture the result.

    Centralized so tests can patch :func:`amc.cli.status._run_ps` and
    inject canned output without spinning up a real ``ps`` process.
    Mirrors the ``_run`` seam used by :mod:`amc.cli.launchctl`.
    """
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["ps", "-o", "lstart=", "-p", pid],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Uptime probing
# ---------------------------------------------------------------------------


# ``ps -o lstart=`` emits the start time as e.g.
# ``Sun May 10 12:34:56 2026``. The trailing ``=`` suppresses the
# header, so the stdout is just the date line. macOS always uses the
# C locale style for this format regardless of the user's LC_*.
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def probe_process_start(pid: str) -> datetime | None:
    """Return the start time of ``pid`` (best-effort) or ``None``.

    Calls ``ps -o lstart= -p <pid>`` and parses the result. Returns
    ``None`` on any failure (non-numeric pid, ps non-zero exit, empty
    stdout, parse failure). Never raises — spec §5.6 Edge Cases
    require the CLI not to crash on a malformed field.
    """
    if not pid or not pid.isdigit():
        return None
    try:
        proc = _run_ps(pid)
    except OSError:
        # ``ps`` missing on PATH (unlikely on macOS but defensive).
        return None
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip()
    if not line:
        return None
    try:
        return datetime.strptime(line, _LSTART_FORMAT)
    except ValueError:
        return None


def humanize_uptime(delta_seconds: float) -> str:
    """Render a non-negative duration as a compact human-readable string.

    Format choices (spec §5.6 AC examples):

    * ``< 60 s`` → ``"42s"``
    * ``< 1 h``  → ``"3m 12s"``
    * ``< 1 d``  → ``"2h 8m"``
    * ``>= 1 d`` → ``"4d"`` (no hours/minutes for very long uptimes)

    Negative deltas (clock skew between ``ps`` and the local clock)
    collapse to ``"-"`` so we never emit a misleading negative
    duration.
    """
    if delta_seconds < 0:
        return "-"
    total = int(delta_seconds)
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days = hours // 24
    return f"{days}d"


def _compute_uptime(
    pid: str,
    *,
    now: Callable[[], datetime],
) -> str:
    """Return the humanized uptime for ``pid`` or ``"-"`` if unavailable."""
    started = probe_process_start(pid)
    if started is None:
        return "-"
    try:
        delta = (now() - started).total_seconds()
    except (TypeError, ValueError):
        # Shouldn't happen — both sides are naive datetimes from the
        # same system clock — but guard so a malformed clock fixture
        # cannot crash the CLI.
        return "-"
    return humanize_uptime(delta)


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def build_row(
    service: Service,
    print_result: PrintResult,
    *,
    enabled_map: dict[str, bool],
    now: Callable[[], datetime],
) -> dict[str, str]:
    """Build the status row dict for a single ``service``.

    ``loaded=no`` collapses every detail field to ``"-"`` per spec §5.6
    Edge Cases — even if launchctl somehow leaked a partial pid line,
    we do not surface it because the service object is not actually
    running in any meaningful sense.

    ``enabled`` defaults to ``"yes"`` when the label is absent from the
    disabled map: launchd's default state for a freshly-bootstrapped
    service is enabled.
    """
    loaded = "yes" if print_result.loaded else "no"

    if not print_result.loaded:
        # Disabled state is still meaningful — an operator may have
        # bootout'd a service but kept its disable flag set. Keep that
        # information; everything else is N/A.
        enabled_bool = enabled_map.get(service.label, True)
        return {
            "service": service.name,
            "loaded": loaded,
            "enabled": "yes" if enabled_bool else "no",
            "pid": "-",
            "last_exit": "-",
            "uptime": "-",
        }

    enabled_bool = enabled_map.get(service.label, True)
    pid = print_result.pid
    uptime = _compute_uptime(pid, now=now) if pid not in {"-", "unknown"} else "-"

    return {
        "service": service.name,
        "loaded": loaded,
        "enabled": "yes" if enabled_bool else "no",
        "pid": pid,
        "last_exit": print_result.last_exit,
        "uptime": uptime,
    }


# ---------------------------------------------------------------------------
# Public columns (also used by tests)
# ---------------------------------------------------------------------------


STATUS_COLUMNS: tuple[dict[str, str], ...] = (
    {"id": "service", "header": "service"},
    {"id": "loaded", "header": "loaded"},
    {"id": "enabled", "header": "enabled"},
    {"id": "pid", "header": "pid"},
    {"id": "last_exit", "header": "last_exit"},
    {"id": "uptime", "header": "uptime"},
)


# ---------------------------------------------------------------------------
# Service resolution
# ---------------------------------------------------------------------------


def _services_for(target: str) -> list[Service]:
    """Resolve the ``[name|all]`` argument to a service list.

    Mirrors the pattern used by :mod:`amc.cli.logs` so an empty string
    or the literal ``"all"`` fans out across the registry. Anything
    else goes through :func:`resolve_targets`, which raises
    :class:`UnknownServiceError` on bad input (mapped to exit 1 by the
    command body).
    """
    if not target or target == "all":
        return list(REGISTRY)
    return resolve_targets([target])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_status(
    target: str,
    *,
    json_mode: bool = False,
    stream: IO[str] | None = None,
    err_stream: IO[str] | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> int:
    """Execute the ``amc status`` workflow and return the process exit code.

    Args:
        target: ``"adapter"``, ``"receiver"``, ``"backup"``, or
            ``"all"``. Empty string is treated as ``"all"``.
        json_mode: Emit a JSON array instead of the table.
        stream: Output sink (defaults to :data:`sys.stdout`).
        err_stream: Error sink (defaults to :data:`sys.stderr`).
        now: Clock injection for deterministic uptime computation in
            tests. Defaults to :func:`datetime.datetime.now`.

    Returns:
        ``0`` if every queried service is loaded; ``1`` on unknown
        service name; ``2`` if any queried service is not loaded.
    """
    if stream is None:
        stream = sys.stdout
    if err_stream is None:
        err_stream = sys.stderr

    try:
        services = _services_for(target)
    except UnknownServiceError as err:
        err_stream.write(f"{err}\n")
        return 1

    # One ``launchctl print-disabled`` call for all services. The helper
    # never raises and returns a partial dict on parse failure, so
    # callers always get a usable mapping.
    enabled_map = print_disabled_state()

    rows: list[dict[str, str]] = []
    all_loaded = True
    for svc in services:
        result = print_service(svc.label)
        if not result.loaded:
            all_loaded = False
        rows.append(
            build_row(svc, result, enabled_map=enabled_map, now=now),
        )

    # Lazy-import to keep ``amc --help`` cold start under 200 ms
    # (spec §6.1) — Rich is the heaviest dependency in the helper.
    from amc.cli.output import render_table

    render_table(rows, list(STATUS_COLUMNS), json_mode=json_mode, stream=stream)

    return 0 if all_loaded else 2
