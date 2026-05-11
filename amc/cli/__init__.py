"""AMC command-line interface (Typer-based).

The CLI lives inside the root ``amc`` package so it can import freely from
``amc.*`` without dependency gymnastics. The top-level Typer application is
exposed as :data:`amc.cli.app.app` and wired as the ``amc`` console script
entry point in ``pyproject.toml``.

Keep this module light: avoid heavy imports here so ``amc --help`` stays
under the 200 ms cold-start target (spec §6.1).
"""

from amc.cli.app import app

__all__ = ["app"]
