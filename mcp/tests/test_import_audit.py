"""Static guard test for ``scripts/import_audit.py``.

Mirrors ``mcp-wrapper/tests/import-audit.test.ts``: covers the helpers and
the exit-code contract. The real ``amg_mcp`` source tree is also walked to
prove no platform-specific imports leaked in.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "import_audit.py"
PACKAGE_DIR = REPO / "src" / "amg_mcp"


def _load_module():
    spec = importlib.util.spec_from_file_location("import_audit_module", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_audit_module"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit_mod():
    return _load_module()


class TestExtractImportSpecifiers:
    def test_handles_plain_imports(self, audit_mod) -> None:
        src = "import os\nimport sys"
        names = [n for n, _ in audit_mod.extract_import_specifiers(src)]
        assert "os" in names and "sys" in names

    def test_handles_from_imports(self, audit_mod) -> None:
        src = "from amg_mcp.tools import context"
        names = [n for n, _ in audit_mod.extract_import_specifiers(src)]
        assert names == ["amg_mcp.tools"]

    def test_handles_aliased_imports(self, audit_mod) -> None:
        src = "import discord as d"
        names = [n for n, _ in audit_mod.extract_import_specifiers(src)]
        assert names == ["discord"]

    def test_ignores_string_literals(self, audit_mod) -> None:
        # The audit only looks at imports — a string mentioning "discord"
        # must not produce a hit.
        src = textwrap.dedent(
            """
            DESCRIPTION = "This tool talks to Discord channels."
            import httpx
            """
        )
        names = [n for n, _ in audit_mod.extract_import_specifiers(src)]
        assert names == ["httpx"]

    def test_skips_invalid_python(self, audit_mod) -> None:
        # Non-parseable source returns no hits rather than raising.
        assert audit_mod.extract_import_specifiers("not python @@ syntax") == []


class TestFindForbiddenToken:
    def test_matches_known_tokens(self, audit_mod) -> None:
        assert audit_mod.find_forbidden_token("discord.client") == "discord"
        assert audit_mod.find_forbidden_token("amg.connectors.imessage") == "imessage"
        assert audit_mod.find_forbidden_token("subprocess_osascript") == "osascript"

    def test_case_insensitive(self, audit_mod) -> None:
        assert audit_mod.find_forbidden_token("AppleScript_helpers") == "applescript"

    def test_returns_none_for_safe_specifiers(self, audit_mod) -> None:
        assert audit_mod.find_forbidden_token("amg_mcp.http_client") is None
        assert audit_mod.find_forbidden_token("httpx") is None


class TestAuditPackage:
    def test_real_package_is_clean(self, audit_mod) -> None:
        violations = audit_mod.audit_package(PACKAGE_DIR)
        assert violations == []

    def test_detects_planted_violation(self, audit_mod, tmp_path: Path) -> None:
        pkg = tmp_path / "amg_mcp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "bad.py").write_text("import discord\n")
        violations = audit_mod.audit_package(pkg)
        assert len(violations) == 1
        v = violations[0]
        assert v.specifier == "discord"
        assert v.token == "discord"


class TestExitCodes:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_exit_zero_when_clean(self) -> None:
        result = self._run(str(PACKAGE_DIR))
        assert result.returncode == 0
        assert "OK" in result.stdout

    def test_exit_one_on_violation(self, tmp_path: Path) -> None:
        pkg = tmp_path / "amg_mcp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "bad.py").write_text("from chat.db import x\n")
        result = self._run(str(pkg))
        assert result.returncode == 1
        assert "FAIL" in result.stderr

    def test_exit_two_on_missing_directory(self, tmp_path: Path) -> None:
        result = self._run(str(tmp_path / "does-not-exist"))
        assert result.returncode == 2
