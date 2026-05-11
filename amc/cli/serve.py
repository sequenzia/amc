"""Implementation of ``amc serve`` (spec §5.2).

This module is import-light by design — the top-level ``amc/cli/app.py``
defers importing it until the ``serve`` command is actually invoked, which
preserves the sub-200 ms cold-start budget (spec §6.1). At runtime the
command does ``os.execvp`` so the uvicorn process **replaces** the Python
CLI process entirely. That keeps the process tree flat (no extra wrapper
PID), and forwards Ctrl-C / SIGINT straight to uvicorn with no signal
relay needed.
"""

from __future__ import annotations

import os
import sys

__all__ = ["run_serve"]


# Default bind addresses per spec §5.2.
_DEFAULTS: dict[str, tuple[str, int]] = {
    "adapter": ("127.0.0.1", 8080),
    "receiver": ("127.0.0.1", 8090),
}


def _build_argv(target: str, host: str, port: int) -> list[str]:
    """Return the exact ``uv`` argv vector for the given target.

    The vectors here match spec §5.2 verbatim; tests assert against them.
    """
    if target == "adapter":
        return [
            "uv",
            "run",
            "uvicorn",
            "amc.app:app",
            "--host",
            host,
            "--port",
            str(port),
        ]
    if target == "receiver":
        return [
            "uv",
            "run",
            "--project",
            "webhook-receiver",
            "uvicorn",
            "amc_receiver.app:app",
            "--host",
            host,
            "--port",
            str(port),
        ]
    # The caller should have rejected unknown targets before reaching here;
    # this is a defensive guard rather than a user-facing branch.
    raise ValueError(f"unknown serve target: {target!r}")


def run_serve(target: str, host: str | None, port: int | None) -> int:
    """Exec ``uv run ...`` for ``target`` (adapter|receiver) or refuse ``backup``.

    Returns an exit code only on the early-refusal path for ``backup`` or
    when ``uv`` is missing on PATH. On the happy path this function does
    not return — ``os.execvp`` replaces the current process image.
    """
    if target == "backup":
        # Spec §5.2: backup is launchd-only; document the right command.
        print(
            "backup runs only under launchd; use 'amc service start backup'",
            file=sys.stderr,
        )
        return 1

    if target not in _DEFAULTS:
        # Defensive: Typer's ``Argument`` validation should reject unknowns,
        # but keep this branch so a programmatic caller gets a clear error.
        print(
            f"unknown serve target: {target!r} (expected adapter, receiver, or backup)",
            file=sys.stderr,
        )
        return 2

    default_host, default_port = _DEFAULTS[target]
    resolved_host = host if host is not None else default_host
    resolved_port = port if port is not None else default_port

    argv = _build_argv(target, resolved_host, resolved_port)

    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        print("uv not found on PATH; install uv first", file=sys.stderr)
        return 2
    # Unreachable: execvp either replaces the process or raises.
    return 0  # pragma: no cover
