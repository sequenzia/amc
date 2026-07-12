"""Tool registrations for the AMG MCP wrapper."""

from amg_mcp.tools.context import register_get_message_context
from amg_mcp.tools.list_unread import register_list_unread_messages
from amg_mcp.tools.mark_read import register_mark_read
from amg_mcp.tools.send import register_send_message

__all__ = [
    "register_get_message_context",
    "register_list_unread_messages",
    "register_mark_read",
    "register_send_message",
]
