"""Shim that pretends to be the ``claude`` CLI for end-to-end tests.

Reads stdin (the user message), captures argv + stdin + selected env vars
to a JSON file, and exits ``0``. Tests assert against that file to verify
the runner is invoking ``claude -p`` with the right shape.

Usage::

    invocation_log = tmp_path / "invocations.jsonl"
    fake_claude_path = make_fake_claude(tmp_path, invocation_log)
    monkeypatch.setenv("AMG_RECEIVER_CLAUDE_BIN", str(fake_claude_path))
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Env var the tests set to point us at the JSONL log file.
_LOG_ENV = "FAKE_CLAUDE_LOG"


def main() -> int:
    log_path = os.environ.get(_LOG_ENV)
    stdin_data = sys.stdin.read()
    record = {
        "argv": sys.argv[1:],
        "stdin": stdin_data,
        "env": {
            k: v
            for k, v in os.environ.items()
            if k.startswith("AMG_")
            or k.startswith("FAKE_CLAUDE_")
            or k.startswith("CLAUDE_CODE_")
            or k in ("ENABLE_TOOL_SEARCH", "CLAUDECODE")
        },
        "cwd": os.getcwd(),
    }
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    # Emit a fake "json" output-format envelope so the runner can parse it.
    sys.stdout.write(json.dumps({"type": "result", "result": "ok"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())


def make_fake_claude(tmp_path: Path, invocation_log: Path) -> Path:
    """Drop a thin executable wrapper that runs this module via Python.

    Returns the path tests should pass as ``AMG_RECEIVER_CLAUDE_BIN``.
    """
    script_path = tmp_path / "fake-claude"
    fake_claude_module = Path(__file__).resolve()
    script_path.write_text(
        f"""#!/bin/sh
exec env {_LOG_ENV}={invocation_log} {sys.executable} {fake_claude_module} "$@"
""",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    return script_path
