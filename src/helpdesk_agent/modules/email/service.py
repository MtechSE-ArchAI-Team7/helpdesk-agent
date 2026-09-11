from __future__ import annotations

from typing import Any

from ...config import MCPConfig
from ...mcp import MCPProxyAdapter, MCPResponse


class ZohoMailFlow:
    """Handle email send/reply/read operations via the Zoho Mail MCP server.

    Tool names are "<prefix><ToolName>" (default prefix "ZohoMail_"). Arguments
    follow the server's schema: path_variables for accountId/messageId, a body
    object for content, and query_params for listing.

    sendReplyEmail requires Zoho's internal numeric messageId. When the inbound
    webhook carries an RFC822 Message-ID header (e.g. "<abc@mail.gmail.com>")
    instead, we fall back to a fresh sendEmail with the ticket-ID subject.
    """

    def __init__(
        self,
        mcp_adapter: MCPProxyAdapter | None = None,
        config: MCPConfig | None = None,
    ) -> None:
        self.mcp_adapter = mcp_adapter
        self.config = config or (mcp_adapter.config if mcp_adapter else MCPConfig.from_env())
        self._account_id: str | None = None

    def _tool(self, name: str) -> str:
        return f"zohomail.{self.config.zohomail_tool_prefix}{name}"

    @staticmethod
    def _is_zoho_message_id(message_id: str) -> bool:
        """Zoho's internal message IDs are numeric; RFC822 headers are not."""
        return message_id.strip().isdigit()

    async def _get_account_id(self) -> str | None:
        """Discover the Zoho account ID via getMailAccounts (cached)."""
        if self._account_id is not None:
            return self._account_id
        response: MCPResponse = await self.mcp_adapter.call_tool(  # type: ignore[union-attr]
            self._tool("getMailAccounts"), {}, wait_for_completion=True
        )
        if response.status != "completed":
            return None
        content = response.content
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
        """Send an email via Zoho Mail MCP (ZohoMail_sendEmail)."""
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "message_id": f"mock-{subject[:10].replace(' ', '-')}",
                "timestamp": "2024-01-01T00:00:00Z",
            }

        account_id = await self._get_account_id()
        if not account_id:
            return {"status": "failed", "error": "could not resolve Zoho accountId via getMailAccounts"}

        arguments: dict[str, Any] = {
            "path_variables": {"accountId": account_id},
            "body": {
                "fromAddress": self.config.zohomail_from_address,
                "toAddress": to,
                "subject": subject,
                "content": body,
                "mailFormat": "html" if html_body else "plaintext",
            },
        }

        response: MCPResponse = await self.mcp_adapter.call_tool(
            self._tool("sendEmail"),
            arguments,
            wait_for_completion=True,
            idempotency_key=f"send-email-{subject}",
        )

        if response.status != "completed":
            return {"status": "failed", "action_id": response.action_id, "error": response.error}

        message_id = response.content.get("messageId") or response.content.get("message_id") or f"msg-{response.action_id}"
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
        to: str | None = None,
    ) -> dict[str, Any]:
        """Reply to an email thread via Zoho Mail MCP (ZohoMail_sendReplyEmail).

        Requires Zoho's internal numeric messageId; if the given ID is an
        RFC822 header, falls back to a fresh sendEmail (keeping the subject
        override so the ticket ID still lands in the title).
        """
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "reply_message_id": f"reply-{message_id}",
                "subject": subject,
                "timestamp": "2024-01-01T00:00:00Z",
            }

        account_id = await self._get_account_id()
        if not account_id:
            return {"status": "failed", "error": "could not resolve Zoho accountId via getMailAccounts"}

        if not self._is_zoho_message_id(message_id):
            # Not Zoho's internal ID — cannot thread a reply; send fresh instead.
            if to and subject:
                result = await self.send_email(to=to, subject=subject, body=body)
                result["note"] = "sent as new email: inbound message_id is not a Zoho internal ID"
                return result
            return {"status": "failed", "error": "message_id is not a Zoho internal ID and no fallback recipient/subject given"}

        reply_body: dict[str, Any] = {
            "action": "reply",
            "fromAddress": self.config.zohomail_from_address,
            "content": body,
        }
        if to:
            reply_body["toAddress"] = to
        if subject is not None:
            reply_body["subject"] = subject

        arguments: dict[str, Any] = {
            "path_variables": {"accountId": account_id, "messageId": message_id.strip()},
            "body": reply_body,
        }

        response: MCPResponse = await self.mcp_adapter.call_tool(
            self._tool("sendReplyEmail"),
            arguments,
            wait_for_completion=True,
            idempotency_key=f"reply-{message_id}-{subject or 'default'}",
        )

        if response.status != "completed":
            return {"status": "failed", "action_id": response.action_id, "error": response.error}

        return {
            "status": "sent",
            "reply_message_id": response.content.get("messageId") or response.content.get("message_id"),
            "timestamp": response.content.get("timestamp"),
            "action_id": response.action_id,
        }

    async def get_messages(
        self,
        query: str = "is:unread",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Fetch messages via Zoho Mail MCP (ZohoMail_listEmails)."""
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "messages": [],
                "count": 0,
            }

        account_id = await self._get_account_id()
        if not account_id:
            return {"status": "failed", "error": "could not resolve Zoho accountId via getMailAccounts"}

        arguments: dict[str, Any] = {
            "path_variables": {"accountId": account_id},
            "query_params": {
                "limit": max_results,
                "status": "unread" if "unread" in query else "all",
            },
        }

        response: MCPResponse = await self.mcp_adapter.call_tool(
            self._tool("listEmails"),
            arguments,
            wait_for_completion=True,
        )

        if response.status != "completed":
            return {"status": "failed", "action_id": response.action_id, "error": response.error}

        messages = response.content.get("messages") or response.content.get("data") or []
        return {
            "status": "success",
            "messages": messages,
            "count": len(messages),
            "action_id": response.action_id,
        }
