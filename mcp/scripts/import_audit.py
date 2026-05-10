#!/usr/bin/env python3
"""Static guard: scan the ``amc_mcp`` source tree for forbidden imports.

Spec §9.3 requires "Wrapper has zero platform-specific imports" — this
script is the static enforcement, runnable outside pytest so CI / hand
checks can invoke it directly.

Usage::

    python scripts/import_audit.py [package-dir]

Default package dir is ``<repo>/src/amc_mcp``. The audit parses each ``.py``
with ``ast`` and inspects only module specifiers — a docstring or tool
description that mentions e.g. "Discord" never produces a false positive.

Exit codes::

    0 — clean
    1 — one or more forbidden imports found (printed to stderr)
    2 — usage / IO error (e.g. package dir missing)
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_TOKENS: tuple[str, ...] = (
    "discord",
    "applescript",
    "osascript",
    "chat.db",
    "imessage",
)


@dataclass(frozen=True)
class Violation:
    file: Path
    specifier: str
    token: str
    line: int


def extract_import_specifiers(source: str) -> list[tuple[str, int]]:
    """Return ``(specifier, lineno)`` for every ``import`` / ``from … import``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, node.lineno))
    return out


def find_forbidden_token(specifier: str, tokens: Iterable[str] = FORBIDDEN_TOKENS) -> str | None:
    """Return the first matching forbidden token in ``specifier``, or None."""
    lower = specifier.lower()
    for token in tokens:
        if token in lower:
            return token
    return None


def audit_package(package_dir: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(package_dir.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for specifier, line in extract_import_specifiers(source):
            token = find_forbidden_token(specifier)
            if token is not None:
                violations.append(Violation(file=path, specifier=specifier, token=token, line=line))
    return violations


def _default_package_dir() -> Path:
    here = Path(__file__).resolve().parent
    return here.parent / "src" / "amc_mcp"


def main(argv: list[str]) -> int:
    explicit = argv[1] if len(argv) > 1 else None
    package_dir = Path(explicit).resolve() if explicit else _default_package_dir()

    if not package_dir.exists():
        sys.stderr.write(
            f"[import-audit] package directory not found: {package_dir}\n"
            "Pass an explicit path: python scripts/import_audit.py <package-dir>\n"
        )
        return 2
    if not package_dir.is_dir():
        sys.stderr.write(f"[import-audit] not a directory: {package_dir}\n")
        return 2

    violations = audit_package(package_dir)
    if not violations:
        sys.stdout.write(f"[import-audit] OK — no forbidden imports under {package_dir}\n")
        return 0

    sys.stderr.write(f"[import-audit] FAIL — {len(violations)} forbidden import(s) found:\n")
    for v in violations:
        sys.stderr.write(
            f"  {v.file}:{v.line}  imports {v.specifier!r}  (matched token: {v.token!r})\n"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
