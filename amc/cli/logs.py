"""``amc logs`` — tail-follow AMC service log files (spec §5.7).

Resolves the right log file for a given service, dumps the last N lines,
and (unless ``--no-follow`` is set) keeps the file open and polls for new
content until interrupted with Ctrl-C.

File selection (spec §5.7):

* App logs live in ``~/Library/Logs/messaging-agent/``.
* The default file for service ``<name>`` is today's
  ``<name>-YYYY-MM-DD.log`` based on the local system clock. If that file
  does not yet exist, fall back to the newest file matching
  ``<name>-*.log``.
* ``--launchd`` switches to the launchd-level stdout / stderr files
  recorded in the service registry (``Service.launchd_stdout`` /
  ``Service.launchd_stderr``). Both are tailed together for that mode.
* ``all`` fans out across every registered service. Each output line is
  prefixed ``[<service>] `` so the interleaved stream stays readable.

The module is import-light: only ``datetime``, ``pathlib``, ``signal``,
``sys``, ``time``, and the local ``services`` module are touched at
import time. ``typer`` itself is imported lazily inside the command body
to keep the CLI cold-start budget intact (spec §6.1).
"""

from __future__ import annotations

import contextlib
import signal
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from datetime import date
from pathlib import Path
from typing import IO

from amc.cli.services import REGISTRY, Service, UnknownServiceError, resolve_targets

__all__ = [
    "LOG_DIR",
    "LogsError",
    "resolve_app_log",
    "resolve_launchd_logs",
    "read_last_n_lines",
    "follow",
    "run_logs",
]


# Default log directory. Resolved at import time because ``Path.expanduser``
# does not invoke any heavyweight machinery and the path is a constant.
LOG_DIR: Path = Path("~/Library/Logs/messaging-agent").expanduser()

# Tail-follow poll interval. Matches the "negligible overhead over tail -F"
# performance bar in spec §6.1.
_FOLLOW_INTERVAL_SECONDS: float = 0.25


class LogsError(Exception):
    """Raised when the logs command cannot produce output.

    The command body catches this and maps it to ``typer.Exit(code=1)``
    with the error message written to stderr.
    """


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_app_log(
    service: Service,
    *,
    log_dir: Path = LOG_DIR,
    today: Callable[[], date] = date.today,
) -> Path | None:
    """Return the app-log path for ``service``, or ``None`` if none exist.

    Tries today's ``<name>-YYYY-MM-DD.log`` first; if missing, falls back
    to the newest file matching ``<name>-*.log`` in ``log_dir``. ``None``
    is returned when nothing matches — callers map this to the canonical
    "No logs found" error message.

    ``today`` is injected (defaults to :func:`datetime.date.today`) so
    tests can pin the date without monkeypatching the stdlib.
    """
    today_path = log_dir / f"{service.name}-{today().isoformat()}.log"
    if today_path.is_file():
        return today_path

    # Fallback: newest matching file by modification time.
    candidates = [p for p in log_dir.glob(service.app_log_glob) if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def resolve_launchd_logs(service: Service) -> list[Path]:
    """Return the launchd-level stdout/stderr paths that currently exist.

    Spec §5.7: ``--launchd`` tails the launchd-level files. Both stdout
    and stderr are returned in order so the caller can interleave them.
    Non-existent files are skipped silently; an empty list indicates the
    service has never been started under launchd.
    """
    paths = [service.launchd_stdout, service.launchd_stderr]
    return [p for p in paths if p.is_file()]


# ---------------------------------------------------------------------------
# Reading + following
# ---------------------------------------------------------------------------


def read_last_n_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path`` (each retains its trailing
    newline if present).

    Implemented with :class:`collections.deque` so memory stays bounded
    even when the log file is huge. ``n <= 0`` returns ``[]``.
    """
    if n <= 0:
        return []

    # ``deque(..., maxlen=n)`` keeps only the last n items as we iterate,
    # so we never load the full file into memory.
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        tail = deque(fp, maxlen=n)
    return list(tail)


def _emit(line: str, prefix: str, stream: IO[str]) -> None:
    """Write ``line`` to ``stream`` with the optional ``prefix``.

    ``line`` may or may not end with ``\\n``. We pass it through verbatim
    so we don't double-newline. If the line is empty, we still emit the
    prefix so blank lines remain visible in interleaved output.
    """
    if prefix:
        stream.write(prefix)
    stream.write(line)
    if not line.endswith("\n"):
        stream.write("\n")
    stream.flush()


def follow(
    paths: list[Path],
    *,
    prefix: str = "",
    stream: IO[str] | None = None,
    interval: float = _FOLLOW_INTERVAL_SECONDS,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Tail-follow one or more files, writing new lines to ``stream``.

    All files are opened once, seeked to EOF, and polled at ``interval``
    seconds. New content from any file is written to ``stream`` (default
    :data:`sys.stdout`) prefixed with ``prefix``. Returns when ``stop``
    returns ``True`` — used by tests; production callers run until
    SIGINT, which raises :class:`KeyboardInterrupt` and is caught by the
    command body.
    """
    if stream is None:
        stream = sys.stdout
    if stop is None:
        stop = _never_stop

    handles: list[IO[str]] = []
    try:
        for path in paths:
            fp = path.open("r", encoding="utf-8", errors="replace")
            fp.seek(0, 2)  # SEEK_END — we only want *new* content.
            handles.append(fp)

        # Partial-line buffer per handle. Some processes flush mid-line;
        # we hold incomplete tails until the newline arrives so a prefix
        # never lands in the middle of a logical log line.
        buffers: list[str] = ["" for _ in handles]

        while not stop():
            wrote_any = False
            for idx, fp in enumerate(handles):
                chunk = fp.read()
                if not chunk:
                    continue
                wrote_any = True
                data = buffers[idx] + chunk
                lines = data.splitlines(keepends=True)
                # If the last fragment doesn't end with a newline, hold it
                # back for the next poll.
                if lines and not lines[-1].endswith("\n"):
                    buffers[idx] = lines.pop()
                else:
                    buffers[idx] = ""
                for line in lines:
                    _emit(line, prefix, stream)
            if not wrote_any:
                time.sleep(interval)
    finally:
        for fp in handles:
            fp.close()


def _never_stop() -> bool:
    return False


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


def _services_for(target: str) -> list[Service]:
    """Resolve the CLI's ``[name|all]`` argument to a service list."""
    # An empty string or ``"all"`` fans out across the registry. We route
    # through :func:`resolve_targets` so the typed ``UnknownServiceError``
    # is raised consistently with the rest of the CLI.
    if not target or target == "all":
        return list(REGISTRY)
    return resolve_targets([target])


def _select_paths(
    services: Iterable[Service],
    *,
    launchd: bool,
    log_dir: Path,
    today: Callable[[], date],
) -> list[tuple[Service, list[Path]]]:
    """Return ``[(service, paths)]`` where ``paths`` is the file list to
    tail for that service. May be empty per service."""
    out: list[tuple[Service, list[Path]]] = []
    for svc in services:
        if launchd:
            paths = resolve_launchd_logs(svc)
        else:
            single = resolve_app_log(svc, log_dir=log_dir, today=today)
            paths = [single] if single is not None else []
        out.append((svc, paths))
    return out


def run_logs(
    target: str,
    *,
    launchd: bool = False,
    follow_mode: bool = True,
    last_n: int = 50,
    log_dir: Path | None = None,
    stream: IO[str] | None = None,
    err_stream: IO[str] | None = None,
    today: Callable[[], date] = date.today,
    interval: float = _FOLLOW_INTERVAL_SECONDS,
    stop: Callable[[], bool] | None = None,
) -> int:
    """Execute the ``amc logs`` workflow and return the process exit code.

    This is the testable core; the Typer command body is a thin wrapper
    that parses options and maps the return value to ``typer.Exit``.

    Args:
        target: ``"adapter"``, ``"receiver"``, ``"backup"``, or ``"all"``.
            Empty string is treated as ``"all"``.
        launchd: Tail launchd-level stdout/stderr instead of app logs.
        follow_mode: Keep following after the initial dump (``--no-follow``
            inverts this).
        last_n: Number of trailing lines to emit before following.
        log_dir: Override the app-log directory (tests).
        stream: Output sink; defaults to :data:`sys.stdout`.
        err_stream: Error sink; defaults to :data:`sys.stderr`.
        today: Date provider; defaults to :func:`datetime.date.today`.
        interval: Tail-follow poll interval (seconds).
        stop: Test-only predicate to break the follow loop.

    Returns:
        ``0`` on success, ``1`` on a logs-directory or file-not-found
        error.
    """
    if stream is None:
        stream = sys.stdout
    if err_stream is None:
        err_stream = sys.stderr
    resolved_log_dir = log_dir if log_dir is not None else LOG_DIR

    if not resolved_log_dir.is_dir():
        err_stream.write(
            f"Logs directory not found: {resolved_log_dir}. Has any AMC service run yet?\n"
        )
        return 1

    try:
        services = _services_for(target)
    except UnknownServiceError as err:
        err_stream.write(f"{err}\n")
        return 1

    selections = _select_paths(services, launchd=launchd, log_dir=resolved_log_dir, today=today)

    # Validate up front: if any individual service has no log file, that's
    # an error when the user asked for a specific service. When the user
    # asked for ``all``, a missing per-service log is tolerable as long as
    # at least one service has content (matches the spec intent — running
    # "amc logs all" on a fresh box where only adapter has logged yet
    # should still tail adapter).
    have_any = any(paths for _, paths in selections)
    if not have_any:
        # Pick a representative service name for the message. When the
        # caller targeted a single service, this is unambiguous; for
        # ``all`` we use ``all`` to match spec phrasing.
        label = services[0].name if len(services) == 1 else "all"
        err_stream.write(f"No logs found for {label}. Has it run yet?\n")
        return 1

    multi_service = len(services) > 1

    # ----- initial dump (last N lines) -----
    # When multiple services are in play, each line is prefixed
    # ``[<service>] `` so the interleaved stream stays parseable.
    for svc, paths in selections:
        if not paths:
            # In ``all`` mode, skip services that have no logs yet but
            # surface that fact to the operator so they aren't confused
            # by silence.
            if multi_service:
                err_stream.write(f"No logs found for {svc.name}. Has it run yet?\n")
            continue
        for path in paths:
            prefix = f"[{svc.name}] " if multi_service else ""
            for line in _initial_dump_lines(path, last_n):
                _emit(line, prefix, stream)

    if not follow_mode:
        return 0

    # ----- follow phase -----
    # Flatten ``(service, paths)`` into a flat list of ``(prefix, path)``
    # pairs so the follower can poll all files together.
    follow_specs: list[tuple[str, Path]] = []
    for svc, paths in selections:
        prefix = f"[{svc.name}] " if multi_service else ""
        for path in paths:
            follow_specs.append((prefix, path))

    if not follow_specs:
        return 0

    _follow_multi(follow_specs, stream=stream, interval=interval, stop=stop)
    return 0


def _initial_dump_lines(path: Path, last_n: int) -> Iterator[str]:
    """Yield the trailing ``last_n`` lines of ``path``.

    Errors reading the file are surfaced by raising — the command body
    converts them to ``typer.Exit(1)``. A missing file at this point
    would be a programming error (path resolution should have filtered
    it out) so we let the OSError propagate.
    """
    yield from read_last_n_lines(path, last_n)


def _follow_multi(
    specs: list[tuple[str, Path]],
    *,
    stream: IO[str],
    interval: float,
    stop: Callable[[], bool] | None,
) -> None:
    """Follow multiple ``(prefix, path)`` pairs in one poll loop.

    Mirrors :func:`follow` but lets each file carry its own prefix —
    needed for ``amc logs all`` where each line must be tagged with the
    originating service.
    """
    if stop is None:
        stop = _never_stop

    handles: list[IO[str]] = []
    prefixes: list[str] = []
    try:
        for prefix, path in specs:
            fp = path.open("r", encoding="utf-8", errors="replace")
            fp.seek(0, 2)
            handles.append(fp)
            prefixes.append(prefix)

        buffers: list[str] = ["" for _ in handles]
        while not stop():
            wrote_any = False
            for idx, fp in enumerate(handles):
                chunk = fp.read()
                if not chunk:
                    continue
                wrote_any = True
                data = buffers[idx] + chunk
                lines = data.splitlines(keepends=True)
                if lines and not lines[-1].endswith("\n"):
                    buffers[idx] = lines.pop()
                else:
                    buffers[idx] = ""
                for line in lines:
                    _emit(line, prefixes[idx], stream)
            if not wrote_any:
                time.sleep(interval)
    finally:
        for fp in handles:
            fp.close()


# ---------------------------------------------------------------------------
# SIGINT helper
# ---------------------------------------------------------------------------


def install_sigint_handler() -> None:
    """Install a SIGINT handler that raises :class:`KeyboardInterrupt`.

    Python's default behavior already does this for the main thread, but
    we set it explicitly so the command body's ``except KeyboardInterrupt``
    branch is reachable even if a host environment has muted the signal.
    """

    def _handler(signum: int, frame: object) -> None:  # pragma: no cover
        raise KeyboardInterrupt

    # ``signal.signal`` only works on the main thread. Tests that run in
    # worker threads simply fall back to Python's default handling.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, _handler)
