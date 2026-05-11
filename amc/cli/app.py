"""Typer entry point for the ``amc`` CLI.

This module defines the top-level Typer application plus the empty
subcommand placeholders required by spec §5.9 / §9.1 Phase 1. The
implementations will be filled in by later tasks; this file is intentionally
import-light so that ``amc --help`` cold-starts in under 200 ms
(spec §6.1).

Avoid module-level imports of heavy dependencies (Rich tables, subprocess
helpers, FastAPI app objects, etc.); lazy-import them inside command bodies
when those commands are implemented.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import typer

__all__ = ["app", "service_app"]


def _get_version() -> str:
    """Return the installed package version.

    Prefers ``importlib.metadata`` (works for both editable and built
    installs). Falls back to the package's ``__version__`` attribute, then
    to a sentinel string if neither is available — this should not happen
    in practice but keeps ``amc --version`` from crashing in odd dev setups.
    """
    try:
        return _pkg_version("amc")
    except PackageNotFoundError:
        pass

    try:
        import amc  # local import to keep cold start light

        return getattr(amc, "__version__", "0+unknown")
    except Exception:  # pragma: no cover — defensive
        return "0+unknown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(_get_version())
        raise typer.Exit()


app = typer.Typer(
    name="amc",
    help="Agent Messaging Channel — manage the adapter, receiver, and backup launchd services.",
    no_args_is_help=True,
    # ``add_completion=True`` exposes Typer's built-in ``--install-completion``
    # and ``--show-completion`` options on the top-level app (spec §12 Open
    # Question #2). The sub-apps keep ``add_completion=False`` because Typer
    # only needs the flags on the root command; completion of subcommands is
    # provided automatically by the installed shell script.
    add_completion=True,
)

service_app = typer.Typer(
    name="service",
    help="Manage launchd lifecycle for a single AMC service.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(service_app, name="service")


@app.callback()
def main(
    version: bool = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the amc package version and exit.",
    ),
) -> None:
    """Agent Messaging Channel CLI."""


# ---------------------------------------------------------------------------
# Top-level command placeholders
#
# Each command currently raises :class:`typer.Exit` with a "not yet
# implemented" message so the help surface is complete (spec §5.9) without
# pretending the subcommands work. Later tasks replace these bodies with
# real logic.
# ---------------------------------------------------------------------------


def _not_implemented(name: str) -> typer.Exit:
    typer.echo(f"amc {name}: not yet implemented", err=True)
    return typer.Exit(code=1)


@app.command("serve")
def serve(
    target: str = typer.Argument(
        ...,
        metavar="TARGET",
        help="Which service to run in the foreground: adapter | receiver | backup.",
    ),
    host: str = typer.Option(
        None,
        "--host",
        help="Override the bind host (defaults: adapter=127.0.0.1, receiver=127.0.0.1).",
    ),
    port: int = typer.Option(
        None,
        "--port",
        help="Override the bind port (defaults: adapter=8080, receiver=8090).",
    ),
) -> None:
    """Run an AMC service in the foreground (adapter or receiver).

    Execs ``uv run uvicorn ...`` so the uvicorn process *replaces* the CLI
    process — keeps the tree flat and forwards Ctrl-C directly. The
    ``backup`` target is launchd-only and exits with a clear message.

    Examples:

      amc serve adapter                   # bind 127.0.0.1:8080
      amc serve receiver --port 9090      # override receiver port
    """
    # Lazy import to keep ``amc --help`` cold-start under 200 ms (spec §6.1).
    from amc.cli.serve import run_serve

    exit_code = run_serve(target, host, port)
    raise typer.Exit(code=exit_code)


@app.command("install")
def install(
    names: list[str] = typer.Argument(  # noqa: B008 — Typer argument-default idiom
        None,
        metavar="[name ...]",
        help="Services to install (adapter, receiver, backup, or 'all'). Defaults to all.",
    ),
) -> None:
    """Render plists and bootstrap AMC launchd services.

    Default (no args) installs adapter, receiver, and backup. Pass one or
    more service names to install only those. Exits 2 on launchctl/lint
    failure, 3 if ``~/Library/LaunchAgents/`` is not writable.

    Examples:

      amc install                          # install all services
      amc install adapter receiver         # subset
    """
    # Lazy import to keep ``amc --help`` cold-start under 200 ms (spec §6.1).
    from amc.cli.install import run_install

    exit_code = run_install(list(names) if names else [])
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("uninstall")
def uninstall(
    targets: list[str] = typer.Argument(  # noqa: B008 — Typer argument-default idiom
        None,
        metavar="[name|all]...",
        help="Services to uninstall: any of 'adapter', 'receiver', 'backup', 'all'. Default: all.",
    ),
    keep_plist: bool = typer.Option(
        False,
        "--keep-plist",
        help="Bootout the service(s) but leave the plist on disk.",
    ),
) -> None:
    """Bootout AMC launchd services and remove their plists.

    Per-target pipeline (spec §5.4):
      1. ``launchctl bootout gui/<uid>/<label>``  (already-unloaded is OK).
      2. Unless ``--keep-plist``, remove ``~/Library/LaunchAgents/<label>.plist``.

    The CLI continues across all targets even if one fails; the final
    exit code is the worst (max) per-target code.

    Examples:

      amc uninstall                        # bootout + remove all plists
      amc uninstall adapter --keep-plist   # bootout adapter, retain plist
    """
    # Lazy imports to keep ``amc --help`` cold-start under 200 ms (spec §6.1).
    from amc.cli.services import UnknownServiceError, resolve_targets
    from amc.cli.uninstall import run_uninstall

    names = list(targets) if targets else ["all"]
    try:
        services = resolve_targets(names)
    except UnknownServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    code = run_uninstall(services, keep_plist=keep_plist)
    if code != 0:
        raise typer.Exit(code=code)


@app.command("status")
def status(
    target: str = typer.Argument(
        "all",
        metavar="[name|all]",
        help="Service to inspect: 'adapter', 'receiver', 'backup', or 'all' (default).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON array instead of the Rich/plain table.",
    ),
) -> None:
    """Show launchd state for the AMC services.

    Examples:

      amc status                  # all services, Rich table on a TTY
      amc status adapter          # one row
      amc status --json           # JSON array (machine-readable)
    """
    # Lazy import to keep ``amc --help`` cold-start under 200 ms (spec §6.1).
    from amc.cli.status import run_status

    code = run_status(target, json_mode=json_output)
    if code != 0:
        raise typer.Exit(code=code)


@app.command("logs")
def logs(
    target: str = typer.Argument(
        "all",
        metavar="[name|all]",
        help="Service to tail: 'adapter', 'receiver', 'backup', or 'all' (default).",
    ),
    launchd: bool = typer.Option(
        False,
        "--launchd",
        help="Tail launchd-level stdout/stderr files instead of app logs.",
    ),
    no_follow: bool = typer.Option(
        False,
        "--no-follow",
        help="Print existing content and exit (do not tail-follow).",
    ),
    last_n: int = typer.Option(
        50,
        "-n",
        "--lines",
        help="Number of trailing lines to emit before following (default 50).",
    ),
) -> None:
    """Tail-follow AMC service logs.

    Examples:

      amc logs adapter            # tail today's adapter-YYYY-MM-DD.log
      amc logs --no-follow -n 20 adapter
      amc logs --launchd adapter  # launchd-level stdout/stderr
      amc logs all                # interleaved across all services
    """
    # Lazy import to keep ``amc --help`` cold-start under 200 ms (spec §6.1).
    from amc.cli.logs import install_sigint_handler, run_logs

    install_sigint_handler()
    try:
        code = run_logs(
            target,
            launchd=launchd,
            follow_mode=not no_follow,
            last_n=last_n,
        )
    except KeyboardInterrupt:
        # Ctrl-C during the initial dump phase. Exit cleanly with code 0
        # per spec §5.7 Error Handling.
        raise typer.Exit(code=0) from None
    if code != 0:
        raise typer.Exit(code=code)


@app.command("doctor")
def doctor(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON array of result objects instead of the Rich/plain table.",
    ),
) -> None:
    """Run preflight diagnostics for the AMC install.

    Exits 0 when every check is ``ok`` or ``warn``; exits 2 if any check
    is ``fail``. See spec §5.8 for the full list of checks.

    Examples:

      amc doctor                  # Rich table (or plain in pipes)
      amc doctor --json           # JSON for tooling / screen readers
    """
    # Lazy import to keep ``amc --help`` cold-start under 200 ms (spec §6.1).
    from amc.cli.doctor import run_doctor

    code = run_doctor(json_mode=json_output)
    if code != 0:
        raise typer.Exit(code=code)


# ---------------------------------------------------------------------------
# `amc service` lifecycle commands (spec §5.5)
#
# Each verb resolves the target (defaults to ``all``), invokes the
# corresponding launchctl helper, prints a per-service result line, and
# exits with the worst per-target code. Implementation lives in
# :mod:`amc.cli.service` — imported lazily to keep ``amc --help`` cold-start
# under 200 ms (spec §6.1).
# ---------------------------------------------------------------------------


_SERVICE_TARGET_HELP = "Service to act on: 'adapter', 'receiver', 'backup', or 'all' (default)."


@service_app.command("start")
def service_start(
    target: str = typer.Argument("all", metavar="[name|all]", help=_SERVICE_TARGET_HELP),
) -> None:
    """Start an AMC service via ``launchctl kickstart`` (auto-bootstraps if needed).

    Examples:

      amc service start                    # start all services
      amc service start adapter            # start one
    """
    from amc.cli.service import run_start

    code = run_start(target)
    if code != 0:
        raise typer.Exit(code=code)


@service_app.command("stop")
def service_stop(
    target: str = typer.Argument("all", metavar="[name|all]", help=_SERVICE_TARGET_HELP),
) -> None:
    """Stop an AMC service via ``launchctl kill SIGTERM``.

    Examples:

      amc service stop                     # stop all services
      amc service stop receiver            # stop one
    """
    from amc.cli.service import run_stop

    code = run_stop(target)
    if code != 0:
        raise typer.Exit(code=code)


@service_app.command("restart")
def service_restart(
    target: str = typer.Argument("all", metavar="[name|all]", help=_SERVICE_TARGET_HELP),
) -> None:
    """Restart an AMC service via ``launchctl kickstart -k``.

    Examples:

      amc service restart                  # restart all services
      amc service restart adapter          # restart one
    """
    from amc.cli.service import run_restart

    code = run_restart(target)
    if code != 0:
        raise typer.Exit(code=code)


@service_app.command("enable")
def service_enable(
    target: str = typer.Argument("all", metavar="[name|all]", help=_SERVICE_TARGET_HELP),
) -> None:
    """Enable an AMC service via ``launchctl enable`` (plist stays on disk).

    Examples:

      amc service enable                   # enable all services
      amc service enable backup            # enable one
    """
    from amc.cli.service import run_enable

    code = run_enable(target)
    if code != 0:
        raise typer.Exit(code=code)


@service_app.command("disable")
def service_disable(
    target: str = typer.Argument("all", metavar="[name|all]", help=_SERVICE_TARGET_HELP),
) -> None:
    """Disable an AMC service via ``launchctl disable``.

    Examples:

      amc service disable                  # disable all services
      amc service disable backup           # disable one
    """
    from amc.cli.service import run_disable

    code = run_disable(target)
    if code != 0:
        raise typer.Exit(code=code)
