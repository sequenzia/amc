"""Unit tests for :class:`ClaudeRunner` env hygiene.

The nested ``claude -p`` must run in a clean, deterministic context: MCP
tool-search forced off (so the four ``mcp__amg__*`` tools load directly at
init instead of being deferred behind ToolSearch), and the outer Claude Code
session vars scrubbed (so the child does not believe it is part of the parent
session when the receiver was launched from inside a Claude Code session).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amg_receiver.claude_runner import ClaudeRunner
from amg_receiver.config import ReceiverConfig


def _config() -> ReceiverConfig:
    return ReceiverConfig(
        bind_host="127.0.0.1",
        bind_port=8090,
        webhook_secret="s" * 16,
        bearer_token="t" * 16,  # noqa: S106 — test fixture
        agent_id="test-agent",
        agent_prompt_file=Path("/nonexistent/prompt.md"),
        dangerous=False,
        claude_timeout_seconds=15,
        log_dir=Path("/tmp/amg-test-logs"),
        idle_worker_ttl_seconds=5,
        dedupe_cache_size=128,
        claude_bin="claude",
        base_url="http://127.0.0.1:8080",
    )


def test_build_env_defaults_tool_search_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    env = ClaudeRunner(_config())._build_env()
    # Without this, MCP tools are deferred and the agent intermittently fails
    # to load them ("server hasn't finished connecting") and never replies.
    assert env["ENABLE_TOOL_SEARCH"] == "false"


def test_build_env_respects_explicit_tool_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "auto")
    env = ClaudeRunner(_config())._build_env()
    assert env["ENABLE_TOOL_SEARCH"] == "auto"


def test_build_env_scrubs_claude_code_session_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer-session")
    monkeypatch.setenv("CLAUDE_CODE_TASK_LIST_ID", "amg")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_ENABLE_TASKS", "1")
    monkeypatch.setenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    env = ClaudeRunner(_config())._build_env()

    assert "CLAUDECODE" not in env
    assert not any(k.startswith("CLAUDE_CODE_") for k in env)


def test_build_env_preserves_unrelated_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # noqa: S105 — test fixture
    # CLAUDE_EFFORT is not a CLAUDE_CODE_ var; it must survive.
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")

    env = ClaudeRunner(_config())._build_env()

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/test"
    assert env["ANTHROPIC_API_KEY"] == "sk-test"  # noqa: S105 — test fixture
    assert env["CLAUDE_EFFORT"] == "xhigh"
