"""AMG command-line interface (Typer-based).

The CLI lives inside the root ``amg`` package so it can import freely from
``amg.*`` without dependency gymnastics. The top-level Typer application is
exposed as :data:`amg.cli.app.app` and wired as the ``amg`` console script
entry point in ``pyproject.toml``.

Keep this module light: avoid heavy imports here so ``amg --help`` stays
under the 200 ms cold-start target (spec §6.1).
"""

from amg.cli.app import app

__all__ = ["app"]
