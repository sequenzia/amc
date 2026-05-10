"""HTTP -> MCP error mapping.

Spec references:
  - §7.4.12 — adapter error envelope: ``{"error": {"code", "message", "details"?}}``
  - §6.1   — MCP tool surface (each tool returns either a normal result or an
             ``isError=True`` payload)

The mapping is intentionally explicit so reviewers can see every exposed
string in one place. Unknown codes fall back to the generic INTERNAL_ERROR
phrasing, embedding the upstream ``message`` so the agent still has
something actionable. Network failures (no HTTP response at all) include
the configured base URL so the agent can suggest a concrete fix.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from amc_mcp.config import get_config
from amc_mcp.http_client import HttpErr

__all__ = [
    "McpErrorResponse",
    "map_http_error_to_mcp_response",
]


@dataclass(frozen=True)
class McpErrorResponse:
    """MCP tool error response shape returned to the runtime.

    ``content`` is a list of ``{"type": "text", "text": ...}`` dicts.
    """

    content: list[dict[str, str]]
    isError: bool = True  # noqa: N815  matches MCP wire field naming


def map_http_error_to_mcp_response(
    error: HttpErr,
    *,
    resolve_base_url: Callable[[], str] | None = None,
) -> McpErrorResponse:
    """Translate an :class:`HttpErr` into the MCP tool error response shape."""
    text = _render_error_text(error, resolve_base_url)
    return McpErrorResponse(content=[{"type": "text", "text": text}], isError=True)


def _render_error_text(
    error: HttpErr,
    resolve_base_url: Callable[[], str] | None,
) -> str:
    if error.code == "NETWORK_ERROR":
        base_url = _safe_base_url(resolve_base_url)
        return f"Cannot reach adapter at {base_url}. Is it running?"

    if error.code == "UNAUTHORIZED":
        return "Bearer token rejected by adapter. Check AMC_BEARER_TOKEN."

    if error.code == "AGENT_ID_REQUIRED":
        return "Internal error: agent id missing — please report."

    if error.code == "MESSAGE_NOT_FOUND":
        return "Message not found or quarantined."

    if error.code == "CHANNEL_NOT_FOUND":
        return (
            "Channel not registered. Send to a channel that has received at "
            "least one inbound message."
        )

    if error.code == "IDEMPOTENCY_KEY_REUSE":
        return "Internal error: idempotency key collision — please retry."

    if error.code == "RATE_LIMITED":
        retry_after = _extract_retry_after(error)
        retry_str = retry_after if retry_after is not None else "a few"
        return f"Rate limited. Retry after {retry_str} seconds."

    if error.code in ("PLATFORM_SEND_FAILED", "SEND_FAILED"):
        return f"Platform delivery failed: {error.message}."

    if error.code == "PLATFORM_AUTH":
        return "Platform authentication failed (e.g., Discord token rejected)."

    if error.code == "ATTACHMENT_TOO_LARGE_FOR_PLATFORM":
        return "Attachment exceeds platform limit. Compress or omit."

    return f"Adapter internal error: {error.message}."


def _extract_retry_after(error: HttpErr) -> str | None:
    """Pull the ``Retry-After`` value out of the §7.4.12 envelope's ``details``."""
    body: Any = error.body
    if not isinstance(body, dict):
        return None
    err_field = body.get("error")
    if not isinstance(err_field, dict):
        return None
    details = err_field.get("details")
    if not isinstance(details, dict):
        return None
    raw = details.get("retry_after")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            return None
        return str(int(raw)) if raw.is_integer() else str(raw)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _safe_base_url(resolve: Callable[[], str] | None) -> str:
    """Resolve the configured base URL without crashing if config is unloaded."""
    if resolve is not None:
        try:
            return resolve()
        except Exception:
            return "AMC_BASE_URL"
    try:
        return get_config().base_url
    except Exception:
        return "AMC_BASE_URL"
