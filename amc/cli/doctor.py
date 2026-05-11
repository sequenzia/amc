"""Preflight diagnostics for the ``amc`` install (spec §5.8).

``amc doctor`` is the operator's first-line answer to "why isn't this
working?". It runs a fixed list of cheap, side-effect-free checks against
the local environment (PATH, filesystem, launchd, listening sockets) and
emits a Rich-formatted result table with three columns: ``check``,
``status`` (``ok`` / ``warn`` / ``fail``), ``details``.

Design choices:

* **One function per check.** Every check returns ``(status, details)``
  where ``status`` is the literal string ``"ok"``, ``"warn"``, or
  ``"fail"``. This keeps each check independently testable with
  ``monkeypatch`` and avoids any per-check state machinery.
* **Runner is exception-safe.** :func:`run_checks` wraps every check in a
  ``try`` block: a raising check yields a synthetic row with
  ``status="fail"``, ``details="<ExceptionClass>: <message>"`` and doctor
  continues with the remaining checks. The CLI never crashes on a single
  bad check (spec §5.8 Error Handling).
* **FDA check is soft.** Reading ``~/Library/Messages/chat.db`` from the
  CLI process tests whether **the CLI binary** has Full Disk Access — not
  the launchd-spawned adapter binary, which is the principal that
  actually needs FDA. A failure here is therefore a ``warn``, not a
  ``fail``, with details that explain the distinction.
* **Output via :func:`amc.cli.output.render_table`.** All format selection
  (Rich / plain / JSON) flows through the shared helper; no inline
  ``Console`` / ``Table`` here.
* **Exit codes**: 0 when every check is ``ok`` or ``warn``; 2 when any
  check is ``fail`` (spec §5.8 AC).

Spec references: §5.8 Doctor, §6.4 Accessibility (``--json``), §7.4
Technical Constraints.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from amc.cli.launchctl import DOMAIN
from amc.cli.output import render_table
from amc.cli.services import REGISTRY, Service

__all__ = [
    "CheckResult",
    "CheckFn",
    "DEFAULT_CHECKS",
    "REPO_ROOT",
    "ENV_PATH",
    "CHAT_DB_PATH",
    "LAUNCH_AGENTS_DIR",
    "LOG_DIR",
    "ADAPTER_PORT",
    "RECEIVER_PORT",
    "check_uv_on_path",
    "check_env_file",
    "check_chat_db_readable",
    "check_plist_templates_exist",
    "check_plists_installed",
    "check_services_bootstrapped",
    "check_port_available",
    "check_log_dir_writable",
    "run_checks",
    "run_doctor",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Repo root: the package lives at ``<repo>/amc/cli/doctor.py``; two parents up.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

ENV_PATH: Path = Path("~/.config/messaging-agent/.env").expanduser()
CHAT_DB_PATH: Path = Path("~/Library/Messages/chat.db").expanduser()
LAUNCH_AGENTS_DIR: Path = Path("~/Library/LaunchAgents").expanduser()
LOG_DIR: Path = Path("~/Library/Logs/messaging-agent").expanduser()

ADAPTER_PORT: int = 8080
RECEIVER_PORT: int = 8090


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One row of doctor output.

    Fields:
        check: Short human-readable label, e.g. ``"uv on PATH"``. Used as
            the ``check`` column in both the table and the JSON output.
        status: One of ``"ok"``, ``"warn"``, ``"fail"``. Drives row
            coloring and the final exit code.
        details: Free-form one-line explanation. Empty string is allowed
            but ``"-"`` (the table sentinel) is reserved for "missing
            value" — use a descriptive string instead so the operator has
            something to grep for in the JSON output.
    """

    check: str
    status: str
    details: str


# A check is a no-arg callable returning ``(status, details)``. We deliberately
# do *not* let checks return a :class:`CheckResult` directly — the ``check``
# label is owned by the registration (see :data:`DEFAULT_CHECKS`) so that the
# table column is consistent regardless of how many internal helpers a
# specific check delegates to.
CheckFn = Callable[[], tuple[str, str]]


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

_OK = "ok"
_WARN = "warn"
_FAIL = "fail"

# Valid status strings — accepted by the runner. Anything else is coerced
# to ``fail`` so a misbehaving check function (returning, say, ``"OK"`` or
# ``"FAIL"``) does not slip past status colorization.
_VALID_STATUSES: frozenset[str] = frozenset({_OK, _WARN, _FAIL})


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_uv_on_path() -> tuple[str, str]:
    """Return ``ok`` if ``uv`` is resolvable via :func:`shutil.which`.

    ``uv`` is the workspace runner used by every ``ProgramArguments`` in
    the installed plists and by ``amc serve``. Missing ``uv`` is a hard
    install failure, not a warning.
    """
    found = shutil.which("uv")
    if found is None:
        return _FAIL, "uv not found on PATH; install via https://docs.astral.sh/uv/"
    return _OK, f"found at {found}"


def check_env_file() -> tuple[str, str]:
    """Return ``ok`` if the env file exists and is readable.

    A missing or unreadable ``~/.config/messaging-agent/.env`` means the
    launchd plists will fail with ``EnvironmentFile`` errors or with
    missing bearer-token configuration. This is a hard ``fail``.
    """
    if not ENV_PATH.exists():
        return _FAIL, f"missing: {ENV_PATH}"
    if not os.access(ENV_PATH, os.R_OK):
        return _FAIL, f"not readable: {ENV_PATH}"
    return _OK, f"readable at {ENV_PATH}"


def check_chat_db_readable() -> tuple[str, str]:
    """Return ``warn`` if the CLI process cannot read ``chat.db``.

    The Full Disk Access principal that actually matters is the
    **launchd-spawned adapter binary**, not the CLI. Failing this check
    therefore does not mean FDA is broken — it just means the CLI itself
    lacks FDA, which is normal. We surface a ``warn`` so operators see
    the explanatory message without being scared by a ``fail``.

    We try to open the file for read; a successful open (and immediate
    close) is sufficient — we do not actually read any rows.
    """
    if not CHAT_DB_PATH.exists():
        # An entirely missing chat.db is rare on a real Mac; treat it as a
        # warn so the operator can investigate without doctor blocking
        # CI-style automation.
        return (
            _WARN,
            f"{CHAT_DB_PATH} not found; iMessage may be uninitialised on this account",
        )
    try:
        with open(CHAT_DB_PATH, "rb") as fp:
            fp.read(1)
    except PermissionError:
        return (
            _WARN,
            (
                f"CLI cannot read {CHAT_DB_PATH}; grant Full Disk Access to the "
                "launchd-spawned adapter binary (not this CLI) — see RUNBOOK."
            ),
        )
    except OSError as exc:
        # ENOTSUP / EIO / etc. — surface the OS error but keep it a warn,
        # matching the FDA-explanation contract.
        return _WARN, f"could not open {CHAT_DB_PATH}: {exc.__class__.__name__}: {exc}"
    return _OK, f"readable at {CHAT_DB_PATH}"


def check_plist_templates_exist() -> tuple[str, str]:
    """Return ``ok`` if every registered plist template exists on disk.

    The registry stores **repo-relative** paths for plist templates so
    they remain meaningful in test environments; resolve against
    :data:`REPO_ROOT` for the actual on-disk check. Listing the missing
    templates by short service name keeps the details column scannable.
    """
    missing: list[str] = []
    for svc in REGISTRY:
        path = REPO_ROOT / svc.plist_template
        if not path.is_file():
            missing.append(svc.name)
    if missing:
        return _FAIL, f"missing plist template(s): {', '.join(missing)}"
    return _OK, f"all {len(REGISTRY)} plist templates present under ops/launchd/"


def check_plists_installed() -> tuple[str, str]:
    """Return ``ok`` if every plist is installed under ``~/Library/LaunchAgents/``.

    A clean machine may not have the directory at all yet — that is still
    a ``fail`` (no plists installed), not a crash. The check enumerates
    each missing label so the operator knows which ``amc install`` step
    has not yet been run.
    """
    if not LAUNCH_AGENTS_DIR.is_dir():
        return (
            _FAIL,
            f"{LAUNCH_AGENTS_DIR} does not exist; run 'amc install' to bootstrap services",
        )
    missing: list[str] = []
    for svc in REGISTRY:
        installed = LAUNCH_AGENTS_DIR / f"{svc.label}.plist"
        if not installed.is_file():
            missing.append(svc.label)
    if missing:
        return _FAIL, f"plist(s) not installed: {', '.join(missing)}"
    return _OK, f"all {len(REGISTRY)} plists installed under {LAUNCH_AGENTS_DIR}"


def check_services_bootstrapped(
    *,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str, str]:
    """Return ``ok`` if every service is bootstrapped (``launchctl print`` succeeds).

    ``launchctl print gui/<uid>/<label>`` exits 0 when the service is
    loaded in the domain, non-zero (commonly 113 "Could not find service"
    or 3 "No such process") otherwise. We do not parse the output — only
    the return code matters for this check.

    The ``run`` kwarg is the centralized subprocess seam (mirrors
    :func:`amc.cli.launchctl._run`); tests inject a fake that captures
    ``argv`` and returns canned returncodes per label.
    """
    runner = run if run is not None else _default_run
    unloaded: list[str] = []
    for svc in REGISTRY:
        target = f"{DOMAIN}/{svc.label}"
        argv = ["launchctl", "print", target]
        proc = runner(argv)
        if proc.returncode != 0:
            unloaded.append(svc.label)
    if unloaded:
        return _FAIL, f"not bootstrapped: {', '.join(unloaded)}"
    return _OK, f"all {len(REGISTRY)} services bootstrapped in {DOMAIN}"


def check_port_available(
    port: int,
    *,
    probe: Callable[[int], bool] | None = None,
) -> tuple[str, str]:
    """Return ``ok`` if *port* is **free** on localhost.

    "Free" here means: nothing is currently listening on
    ``127.0.0.1:<port>``. The probe attempts a TCP connect with a short
    timeout; connection failure (``ConnectionRefusedError``) means the
    port is free. Connection success means **something** is listening —
    which may be a *different* process than our adapter/receiver, hence
    a ``fail`` with details pointing at ``lsof`` for the operator.

    The ``probe`` kwarg lets tests inject deterministic results without
    binding real sockets.
    """
    is_in_use = probe(port) if probe is not None else _probe_port_in_use(port)
    if is_in_use:
        return (
            _FAIL,
            (
                f"port {port} already bound; run "
                f"'lsof -nP -iTCP:{port} -sTCP:LISTEN' to identify the owner"
            ),
        )
    return _OK, f"port {port} is free"


def check_log_dir_writable() -> tuple[str, str]:
    """Return ``ok`` if the log dir exists and is writable.

    Both halves matter: a missing directory blocks launchd from writing
    ``StandardOutPath`` (so the service silently fails on first launch);
    a non-writable directory blocks the app loggers. We probe with
    :func:`os.access` to avoid actually creating a file.
    """
    if not LOG_DIR.is_dir():
        return _FAIL, f"missing: {LOG_DIR} (run 'mkdir -p {LOG_DIR}')"
    if not os.access(LOG_DIR, os.W_OK):
        return _FAIL, f"not writable: {LOG_DIR}"
    return _OK, f"writable at {LOG_DIR}"


# ---------------------------------------------------------------------------
# Subprocess + socket seams
# ---------------------------------------------------------------------------


def _default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Default subprocess seam for :func:`check_services_bootstrapped`.

    Centralized so the test suite has a single attribute to patch
    (``amc.cli.doctor._default_run``) when it wants to inject canned
    launchctl output without touching the global :mod:`subprocess`.
    """
    return subprocess.run(  # noqa: S603 — argv is a fixed list, no shell
        argv,
        capture_output=True,
        text=True,
        check=False,
    )


def _probe_port_in_use(port: int, *, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """Return True if *port* is bound by a listener on ``host``.

    We attempt a TCP connect rather than ``bind()``: ``bind`` would
    succeed if the existing socket is bound to a different interface
    (e.g. ``0.0.0.0`` vs ``127.0.0.1``), giving a false negative. A
    short connect timeout protects against listeners that accept but
    never respond.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except (ConnectionRefusedError, TimeoutError):
            return False
        except OSError:
            # Any other socket error (e.g. EHOSTUNREACH on an unusual
            # network config) — treat as "we don't know, assume free" so
            # the doctor row at least does not spuriously claim a
            # conflict. The detailed exit-code check still reflects only
            # confirmed conflicts.
            return False
        return True


# ---------------------------------------------------------------------------
# Default check registration
# ---------------------------------------------------------------------------


# Each entry is ``(check_label, check_fn)``. Order matters: the table is
# rendered in this order so the operator sees the most likely
# misconfigurations first (PATH, env file, FDA), then the launchd checks,
# then the port checks, then the log dir.
DEFAULT_CHECKS: tuple[tuple[str, CheckFn], ...] = (
    ("uv on PATH", check_uv_on_path),
    ("env file readable", check_env_file),
    ("chat.db readable (FDA)", check_chat_db_readable),
    ("plist templates present", check_plist_templates_exist),
    ("plists installed", check_plists_installed),
    ("services bootstrapped", check_services_bootstrapped),
    ("port 8080 free", lambda: check_port_available(ADAPTER_PORT)),
    ("port 8090 free", lambda: check_port_available(RECEIVER_PORT)),
    ("log directory writable", check_log_dir_writable),
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _normalize_status(raw: str) -> str:
    """Return *raw* unchanged if valid, else coerce to ``fail``.

    Misbehaving check functions that return an out-of-band status value
    (e.g. ``"OK"`` capitalised, or ``"error"``) should not silently slip
    past status coloring or exit-code computation. Coercing to ``fail``
    is conservative — the operator sees a clear row and the exit code
    reflects the problem.
    """
    return raw if raw in _VALID_STATUSES else _FAIL


def run_checks(
    checks: Iterable[tuple[str, CheckFn]] = DEFAULT_CHECKS,
) -> list[CheckResult]:
    """Run every check in order, swallowing exceptions per check.

    Each check is invoked inside a ``try`` block. A raising check yields
    a :class:`CheckResult` with ``status="fail"`` and
    ``details="<ExceptionClass>: <msg>"``; doctor continues with the
    next check. This matches the spec §5.8 Edge Case contract.
    """
    results: list[CheckResult] = []
    for label, fn in checks:
        try:
            status, details = fn()
        except Exception as exc:  # noqa: BLE001 — see module docstring rationale
            results.append(
                CheckResult(
                    check=label,
                    status=_FAIL,
                    details=f"{exc.__class__.__name__}: {exc}",
                )
            )
            continue
        results.append(
            CheckResult(
                check=label,
                status=_normalize_status(status),
                details=details,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


# Map status → Rich color for the status cell. Plain/JSON paths ignore
# styling; this is consulted only by the Rich-table render via the
# ``style`` column option in :func:`amc.cli.output.render_table`.
#
# Per-cell styling is not yet supported by ``render_table`` — but the
# column-level ``style`` option **is** supported. We surface coloring
# inline in the status string via Rich markup so the Rich path picks it
# up; plain and JSON paths emit the raw status text unchanged.
_STATUS_COLOR: dict[str, str] = {
    _OK: "green",
    _WARN: "yellow",
    _FAIL: "red",
}


def _maybe_color_status(status: str, *, color_enabled: bool) -> str:
    """Return *status* wrapped in Rich markup when color is enabled.

    The plain renderer in :mod:`amc.cli.output` strips no ANSI on its
    own, so we must not emit Rich markup unless we know it will be
    rendered by Rich. The caller passes ``color_enabled=True`` only when
    output is going to a TTY without ``NO_COLOR`` set.
    """
    if not color_enabled:
        return status
    color = _STATUS_COLOR.get(status)
    if color is None:
        return status
    return f"[{color}]{status}[/]"


def _output_is_rich(stream: IO[str]) -> bool:
    """Return True when *stream* will be rendered via the Rich path.

    Mirrors the TTY + ``NO_COLOR`` checks inside
    :func:`amc.cli.output.render_table`. We replicate them locally so we
    can decide whether to embed Rich markup in the status cells.
    """
    if "NO_COLOR" in os.environ:
        return False
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):
        return False


def _render_results(
    results: list[CheckResult],
    *,
    json_mode: bool,
    stream: IO[str],
) -> None:
    """Emit *results* via :func:`render_table` in the requested format.

    JSON path: emit a list of ``{check, status, details}`` objects with
    the raw status string (no Rich markup) so the JSON is consumable by
    downstream tooling.

    Rich/plain path: build rows where the ``status`` cell carries Rich
    markup when (and only when) the output stream is a TTY without
    ``NO_COLOR``.
    """
    if json_mode:
        rows = [{"check": r.check, "status": r.status, "details": r.details} for r in results]
        render_table(
            rows,
            columns=["check", "status", "details"],
            json_mode=True,
            stream=stream,
        )
        return

    color_enabled = _output_is_rich(stream)
    rows = [
        {
            "check": r.check,
            "status": _maybe_color_status(r.status, color_enabled=color_enabled),
            "details": r.details,
        }
        for r in results
    ]
    render_table(
        rows,
        columns=["check", "status", "details"],
        json_mode=False,
        stream=stream,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _exit_code(results: Iterable[CheckResult]) -> int:
    """Return 2 if any result is ``fail``, else 0.

    The spec is explicit: ``warn`` rows must not block exit 0. Operators
    should be able to wire ``amc doctor`` into CI gating where
    "everything green or yellow" is the success criterion.
    """
    for r in results:
        if r.status == _FAIL:
            return 2
    return 0


def run_doctor(
    *,
    json_mode: bool = False,
    stream: IO[str] | None = None,
    checks: Iterable[tuple[str, CheckFn]] = DEFAULT_CHECKS,
) -> int:
    """Run every check, render the result table, and return an exit code.

    Returns the exit code (0 / 2) rather than raising :class:`typer.Exit`
    so this function stays unit-testable without a CliRunner. The Typer
    wrapper in :mod:`amc.cli.app` translates the return into an exit.

    The ``checks`` kwarg lets tests substitute a smaller registry to
    exercise specific status-mixing scenarios without running every real
    probe.
    """
    out = stream if stream is not None else sys.stdout
    results = run_checks(checks)
    _render_results(results, json_mode=json_mode, stream=out)
    return _exit_code(results)


# ---------------------------------------------------------------------------
# Tiny helper kept for typing symmetry
# ---------------------------------------------------------------------------


# ``Service`` is re-exported from this module so test code can `from
# amc.cli.doctor import Service` if it wants to swap the registry. We do
# not strictly need this for the production path, but it preserves the
# module's contract as a self-contained "doctor" surface.
_ = Service  # silence vulture-style unused-import linters
