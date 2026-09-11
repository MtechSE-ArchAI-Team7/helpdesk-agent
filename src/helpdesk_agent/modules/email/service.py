from __future__ import annotations

from typing import Any

from ...mcp import MCPProxyAdapter, MCPResponse


class ZohoMailFlow:
    """Handle email send/reply/read operations via the Zoho Mail MCP server."""

    def __init__(self, mcp_adapter: MCPProxyAdapter | None = None) -> None:
        self.mcp_adapter = mcp_adapter

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        """Send an email via Zoho Mail MCP."""
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "message_id": f"mock-{subject[:10].replace(' ', '-')}",
                "timestamp": "2024-01-01T00:00:00Z",
            }

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "zohomail.send_email",
            {
                "to": to,
                "subject": subject,
                "body": body,
                "html_body": html_body,
            },
            wait_for_completion=True,
            idempotency_key=f"send-email-{subject}",
        )

        if response.status != "completed":
            return {"status": "failed", "action_id": response.action_id, "error": response.error}

        message_id = response.content.get("message_id") or f"msg-{response.action_id}"
        return {
            "status": "sent",
            "message_id": message_id,
            "timestamp": response.content.get("timestamp", "2024-01-01T00:00:00Z"),
            "action_id": response.action_id,
        }

    async def reply_email(
        self,
        message_id: str,
        body: str,
        subject: str | None = None,
    ) -> dict[str, Any]:
        """Reply to an email via Zoho Mail MCP, optionally overriding the subject."""
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "reply_message_id": f"reply-{message_id}",
                "subject": subject,
                "timestamp": "2024-01-01T00:00:00Z",
            }

        arguments: dict[str, Any] = {"message_id": message_id, "body": body}
        if subject is not None:
            arguments["subject"] = subject

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "zohomail.reply_email",
            arguments,
            wait_for_completion=True,
            idempotency_key=f"reply-{message_id}-{subject or 'default'}",
        )

        if response.status != "completed":
            return {"status": "failed", "action_id": response.action_id, "error": response.error}

        return {
            "status": "sent",
            "reply_message_id": response.content.get("message_id"),
            "timestamp": response.content.get("timestamp"),
            "action_id": response.action_id,
        }

    async def get_messages(
        self,
        query: str = "is:unread",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Fetch messages from Zoho Mail via MCP."""
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "messages": [],
                "count": 0,
            }

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "zohomail.get_messages",
            {
                "query": query,
                "max_results": max_results,
            },
            wait_for_completion=True,
        )

        if response.status != "completed":
            return {"status": "failed", "action_id": response.action_id, "error": response.error}

        return {
            "status": "success",
            "messages": response.content.get("messages", []),
            "count": response.content.get("count", 0),
            "action_id": response.action_id,
        }
