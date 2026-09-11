from __future__ import annotations

from typing import Any

from ...mcp import MCPProxyAdapter, MCPResponse


class ZohoMailFlow:
    """Handle email send/reply/read operations via the Zoho Mail MCP server.

    Tool names follow the Zoho Mail MCP server (sendEmail, sendReplyMail,
    listEmails, getMailAccounts). Argument names follow the Zoho Mail API
    conventions (accountId, toAddress, subject, content, messageId).
    """

    def __init__(self, mcp_adapter: MCPProxyAdapter | None = None) -> None:
        self.mcp_adapter = mcp_adapter
        self._account_id: str | None = None

    async def _get_account_id(self) -> str | None:
        """Discover the Zoho account ID via getMailAccounts (cached)."""
        if self._account_id is not None:
            return self._account_id
        response: MCPResponse = await self.mcp_adapter.call_tool(  # type: ignore[union-attr]
            "zohomail.getMailAccounts", {}, wait_for_completion=True
        )
        if response.status != "completed":
            return None
        content = response.content
        # Accept a few plausible shapes: {accountId}, {accounts: [{accountId}]}
        account_id = content.get("accountId")
        if not account_id:
            accounts = content.get("accounts") or content.get("data") or []
            if accounts and isinstance(accounts, list):
                account_id = accounts[0].get("accountId") or accounts[0].get("account_id")
        if account_id:
            self._account_id = str(account_id)
        return self._account_id

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

        arguments: dict[str, Any] = {
            "toAddress": to,
            "subject": subject,
            "content": body,
        }
        account_id = await self._get_account_id()
        if account_id:
            arguments["accountId"] = account_id

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "zohomail.sendEmail",
            arguments,
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

        arguments: dict[str, Any] = {"messageId": message_id, "content": body}
        if subject is not None:
            arguments["subject"] = subject
        account_id = await self._get_account_id()
        if account_id:
            arguments["accountId"] = account_id

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "zohomail.sendReplyMail",
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

        arguments: dict[str, Any] = {
            "limit": max_results,
            "status": "unread" if "unread" in query else "all",
        }
        account_id = await self._get_account_id()
        if account_id:
            arguments["accountId"] = account_id

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "zohomail.listEmails",
            arguments,
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
