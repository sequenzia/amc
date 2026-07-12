"""The nested agent's tool allowlist must agree with what the wrapper registers.

Four independent strings decide whether the headless ``claude -p`` the webhook
receiver spawns can call anything at all:

1. the ``mcpServers`` key in :func:`build_mcp_config_json` — this is what sets
   the ``mcp__<key>__`` prefix the host generates,
2. :data:`amg_receiver.claude_runner._AMG_TOOLS`, passed to ``--allowedTools``,
3. ``permissions.allow`` in ``claude_workspace/.claude/settings.json``,
4. the tool names the MCP wrapper actually registers with FastMCP.

If any one of them drifts, the failure is silent: ``claude`` starts, every tool
is denied, it exits 0 having produced a polite English answer, and no reply is
ever sent. There is no error in any log. The receiver's own tests assert only
that the ``mcpServers`` key exists — not that the four agree — so nothing else
in the suite would catch it.

This lives in the root suite rather than ``webhook-receiver/tests/`` because it
must import both packages, and ``mcp/scripts/import_audit.py`` forbids the
receiver from importing ``amg_mcp``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from amg_mcp.http_client import HttpOk, HttpResult
from amg_mcp.server import build_server
from amg_receiver.claude_runner import _AMG_TOOLS, WORKSPACE_DIR, build_mcp_config_json
from amg_receiver.config import ReceiverConfig

# Any `mcp__<server>__<tool>` reference. The bundled prompt also mentions the
# glob `mcp__amg__*`, which names no specific tool and is not a drift signal.
_TOOL_REF = re.compile(r"mcp__[a-z0-9]+__[a-z_]+")


class _StubHttpClient:
    """Structurally satisfies the HttpClient Protocol.

    ``build_server`` only closes over the client to register the tools; it is
    never called here, because all we need are the registered tool *names*.
    """

    async def get(self, path: str, query: dict[str, Any] | None = None) -> HttpResult:
        return HttpOk(status=200, data={})

    async def post(
        self,
        path: str,
        body: Any,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        return HttpOk(status=200, data={})

    async def aclose(self) -> None:
        return None


def _receiver_config() -> ReceiverConfig:
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


async def _registered_tool_names() -> set[str]:
    """The unprefixed tool names the wrapper registers with FastMCP."""
    server = build_server(_StubHttpClient())
    return {tool.name for tool in await server.list_tools()}


def _server_key() -> str:
    servers = json.loads(build_mcp_config_json(_receiver_config()))["mcpServers"]
    assert len(servers) == 1, f"expected exactly one MCP server, got {sorted(servers)}"
    return next(iter(servers))


def _workspace_allowlist() -> set[str]:
    settings = json.loads((WORKSPACE_DIR / ".claude" / "settings.json").read_text())
    return set(settings["permissions"]["allow"])


@pytest.mark.asyncio
async def test_allowed_tools_match_what_the_wrapper_registers() -> None:
    """``--allowedTools`` must name exactly the tools that actually exist."""
    key = _server_key()
    expected = {f"mcp__{key}__{name}" for name in await _registered_tool_names()}
    assert set(_AMG_TOOLS) == expected


@pytest.mark.asyncio
async def test_workspace_allowlist_matches_allowed_tools() -> None:
    """The workspace's permissions.allow must not drift from _AMG_TOOLS.

    ``ClaudeRunner`` runs with ``cwd=WORKSPACE_DIR`` and
    ``--setting-sources project,local``, so this file is what the nested agent
    is actually permitted to call.
    """
    assert _workspace_allowlist() == set(_AMG_TOOLS)


@pytest.mark.asyncio
async def test_agent_prompt_names_only_real_tools() -> None:
    """The bundled system prompt must not instruct the agent to call a tool
    that does not exist under the current server key."""
    prompt = Path(__file__).resolve().parents[1].parent / (
        "webhook-receiver/src/amg_receiver/prompts/default_agent_prompt.md"
    )
    named = set(_TOOL_REF.findall(prompt.read_text()))
    assert named, "expected the prompt to name the MCP tools"
    assert named <= set(_AMG_TOOLS), f"prompt names unknown tool(s): {named - set(_AMG_TOOLS)}"
