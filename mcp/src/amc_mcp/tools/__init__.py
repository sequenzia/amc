"""Tool registrations for the AMC MCP wrapper."""

from amc_mcp.tools.context import register_get_message_context
from amc_mcp.tools.list_unread import register_list_unread_messages
from amc_mcp.tools.mark_read import register_mark_read
from amc_mcp.tools.send import register_send_message

__all__ = [
    "register_get_message_context",
    "register_list_unread_messages",
    "register_mark_read",
    "register_send_message",
]
