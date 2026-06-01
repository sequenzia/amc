"""Subprocess wrapper around ``claude -p``.

Each inbound webhook envelope is fed to a fresh ``claude -p`` invocation:

  * **Working directory** is the bundled ``claude_workspace/`` so Claude
    auto-discovers ``.mcp.json`` (the AMC MCP server) and
    ``.claude/settings.json`` (the per-tool allow rules).
  * **System prompt** is loaded from the operator-editable file at
    ``$AMC_RECEIVER_AGENT_PROMPT_FILE`` (default
    ``~/.config/messaging-agent/agent_prompt.md``); a sensible fallback
    ships in the package.
  * **MCP config** is forced to the workspace via ``--strict-mcp-config``
    so user-global ``~/.claude.json`` MCP servers cannot leak into the
    session.
  * **Settings sources** are restricted to ``project,local`` so user-global
    permission grants cannot leak in either.
  * **Permissions** default to the workspace's allowlist for the four
    ``mcp__amc__*`` tools. Setting ``AMC_RECEIVER_DANGEROUS=1`` swaps in
    ``--dangerously-skip-permissions`` for ad-hoc unattended runs.

The envelope JSON plus a brief user-message preamble go to ``claude``'s
stdin in text mode. ``claude`` writes its final assistant text to stdout in
``json`` output format; we log a tail of stdout/stderr along with the
exit code, duration, and message metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from amc_receiver.config import ReceiverConfig

__all__ = [
    "ClaudeRunner",
    "RunResult",
    "WORKSPACE_DIR",
    "build_mcp_config_json",
    "render_user_message",
    "resolve_system_prompt",
]

_log = structlog.get_logger("amc.receiver.runner")

# Bundled workspace directory shipped inside the package. Holds the
# ``.mcp.json`` and ``.claude/settings.json`` that ``claude -p`` discovers
# automatically when invoked with this dir as cwd.
WORKSPACE_DIR: Path = Path(__file__).resolve().parent / "claude_workspace"
DEFAULT_PROMPT_FILE: Path = Path(__file__).resolve().parent / "prompts" / "default_agent_prompt.md"

_AMC_TOOLS = (
    "mcp__amc__list_unread_messages",
    "mcp__amc__send_message",
    "mcp__amc__mark_read",
    "mcp__amc__get_message_context",
)

# Output truncation for log fields — never log full prompt or full reply.
_LOG_TAIL_BYTES = 2048

# --- Environment hygiene for the nested ``claude -p`` --------------------
#
# Claude Code injects these vars into the processes it spawns. When the
# receiver is launched from *inside* an interactive Claude Code session
# (rather than under launchd), ``os.environ`` carries them and they would
# leak into the nested ``claude -p``, where they make that process behave as
# though it were part of the outer session (shared session id, task list,
# tmpdir, SSE port, etc.). Scrub anything matching these so the nested agent
# runs in a clean, predictable context regardless of how the receiver was
# started. Under launchd none of these are set, so the scrub is a no-op.
_SCRUBBED_ENV_EXACT = frozenset({"CLAUDECODE"})
_SCRUBBED_ENV_PREFIXES = ("CLAUDE_CODE_",)

# Force MCP tool-search OFF for the nested agent. With it on — the Claude Code
# default when ``ENABLE_TOOL_SEARCH`` is unset — the four ``mcp__amc__*`` tools
# are *deferred* behind the ToolSearch tool and the ``amc`` server reports
# ``status: pending`` at init. The headless agent then intermittently concludes
# "the MCP server hasn't finished connecting" and ends the turn WITHOUT loading
# or calling the tools, so no reply is ever sent. Setting it to ``false`` loads
# the four tools directly into the tool list at init and makes Claude block on a
# still-connecting server (via WaitForMcpServers) before its first tool call —
# i.e. deterministic. Operators may override by exporting their own value.
# (See https://code.claude.com/docs/en/mcp.md#configure-tool-search.)
_ENV_ENABLE_TOOL_SEARCH = "ENABLE_TOOL_SEARCH"


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int
    duration_ms: float
    stdout_tail: str
    stderr_tail: str
    timed_out: bool


def resolve_system_prompt(prompt_file: Path) -> str:
    """Read the agent prompt from ``prompt_file``; fall back to the bundled default.

    Operators iterate on the prompt by editing the file; the receiver does
    not need to restart for prompt edits to take effect because this runs
    on every invocation.
    """
    try:
        text = prompt_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        _log.warning("agent_prompt_read_failed", path=str(prompt_file), error=str(exc))
        text = ""
    if text:
        return text
    return DEFAULT_PROMPT_FILE.read_text(encoding="utf-8").strip()


def build_mcp_config_json(config: ReceiverConfig) -> str:
    """Return a JSON string suitable for ``claude --mcp-config``.

    Spawns the AMC MCP wrapper using ``sys.executable -m amc_mcp`` so we
    always invoke the wrapper installed in *this* venv (the workspace's
    shared venv when both packages were sync'd together). This avoids the
    PATH-resolution problems that ``"command": "amc-mcp"`` would have under
    launchd's minimal environment.
    """
    return json.dumps(
        {
            "mcpServers": {
                "amc": {
                    "command": sys.executable,
                    "args": ["-m", "amc_mcp"],
                    "env": {
                        "AMC_BEARER_TOKEN": config.bearer_token,
                        "AMC_AGENT_ID": config.agent_id,
                        "AMC_BASE_URL": config.base_url,
                    },
                }
            }
        }
    )


def render_user_message(envelope: dict[str, Any]) -> str:
    """Render the per-message user instruction handed to ``claude -p``.

    The envelope JSON is included verbatim so Claude can call AMC tools
    using ``id`` / ``channel_id`` directly and reason about ``reply_to``
    threading. The preamble is brief — the system prompt carries the
    operator's persona and policies.
    """
    pretty = json.dumps(envelope, indent=2, sort_keys=True)
    return (
        "A new inbound message has arrived. The full normalized envelope "
        "is below. Decide whether and how to respond, then take action via "
        "the AMC MCP tools (`send_message`, `mark_read`, `get_message_context`). "
        "Do not just describe what you would do — actually call the tools.\n\n"
        f"```json\n{pretty}\n```\n"
    )


def _tail(data: bytes, limit: int = _LOG_TAIL_BYTES) -> str:
    if not data:
        return ""
    truncated = data[-limit:]
    return truncated.decode("utf-8", errors="replace")


class ClaudeRunner:
    """Builds and executes the ``claude -p`` command for a single envelope."""

    def __init__(self, config: ReceiverConfig) -> None:
        self._config = config

    def _build_argv(self) -> list[str]:
        argv: list[str] = [
            self._config.claude_bin,
            "-p",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--strict-mcp-config",
            "--mcp-config",
            build_mcp_config_json(self._config),
            "--setting-sources",
            "project,local",
        ]
        if self._config.dangerous:
            argv.append("--dangerously-skip-permissions")
        else:
            argv.append("--allowedTools")
            argv.extend(_AMC_TOOLS)
        return argv

    def _build_env(self) -> dict[str, str]:
        # Inherit the parent process env (uv venv PATH, HOME, ANTHROPIC creds,
        # etc.). The MCP wrapper's own vars are injected via the --mcp-config
        # JSON so we don't leak them into the wider Claude process env here.
        # Scrub the outer Claude Code session vars and force tool-search off so
        # the nested agent runs deterministically (see notes above).
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in _SCRUBBED_ENV_EXACT and not k.startswith(_SCRUBBED_ENV_PREFIXES)
        }
        env.setdefault(_ENV_ENABLE_TOOL_SEARCH, "false")
        return env

    async def run(self, envelope: dict[str, Any]) -> RunResult:
        """Execute ``claude -p`` for ``envelope`` and return a result snapshot."""
        argv = self._build_argv()
        argv.extend(["--system-prompt", resolve_system_prompt(self._config.agent_prompt_file)])
        user_message = render_user_message(envelope)

        loop_started = asyncio.get_running_loop().time()
        log = _log.bind(
            message_id=envelope.get("id"),
            channel_id=envelope.get("channel_id"),
            source=envelope.get("source"),
            dangerous=self._config.dangerous,
        )
        log.info("claude_invocation_start")

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(WORKSPACE_DIR),
            env=self._build_env(),
        )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(user_message.encode("utf-8")),
                timeout=self._config.claude_timeout_seconds,
            )
        except TimeoutError:
            timed_out = True
            with suppress(ProcessLookupError):
                proc.kill()
            stdout_bytes, stderr_bytes = await proc.communicate()
            log.warning(
                "claude_invocation_timeout",
                timeout_seconds=self._config.claude_timeout_seconds,
            )

        duration_ms = round((asyncio.get_running_loop().time() - loop_started) * 1000, 3)
        result = RunResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_ms=duration_ms,
            stdout_tail=_tail(stdout_bytes or b""),
            stderr_tail=_tail(stderr_bytes or b""),
            timed_out=timed_out,
        )

        level = logging.WARNING if (timed_out or result.exit_code != 0) else logging.INFO
        log.log(
            level,
            "claude_invocation_end",
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=timed_out,
            stdout_tail=result.stdout_tail,
            stderr_tail=result.stderr_tail,
        )
        return result
