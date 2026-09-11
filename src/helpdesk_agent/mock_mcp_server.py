from __future__ import annotations

import uuid
from typing import Any

import uvicorn
from fastmcp import FastMCP


def _build_server() -> FastMCP:
    """Create a mock MCP server that mirrors the old JSON-RPC mock's behavior."""
    mcp = FastMCP("helpdesk-mock-mcp")

    @mcp.tool(name="faq.search")
    def faq_search(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {
                "tool": "faq.search",
                "arguments": arguments or {},
                "results": [
                    {
                        "id": "faq-001",
                        "title": "How to reset your password",
                        "body": "To reset your password, go to Settings > Security > Reset Password. "
                        "Follow the on-screen instructions.",
                        "score": 0.95,
                    },
                    {
                        "id": "faq-002",
                        "title": "Troubleshooting login issues",
                        "body": "If you cannot log in, try clearing your browser cache and cookies, "
                        "or use incognito mode.",
                        "score": 0.85,
                    },
                ],
                "count": 2,
            },
        }

    @mcp.tool(name="actions/get")
    def actions_get(action_id: str) -> dict[str, Any]:
        return {
            "status": "completed",
            "action_id": action_id,
            "content": {"observed": True},
        }

    @mcp.tool(name="zohomail.send_email")
    def zohomail_send_email(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {"tool": "zohomail.send_email", "arguments": arguments or {}, "message_id": f"msg-{uuid.uuid4().hex[:8]}"},
        }

    @mcp.tool(name="zohomail.reply_email")
    def zohomail_reply_email(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {"tool": "zohomail.reply_email", "arguments": arguments or {}, "message_id": f"reply-{uuid.uuid4().hex[:8]}"},
        }

    @mcp.tool(name="zohomail.get_messages")
    def zohomail_get_messages(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {
                "tool": "zohomail.get_messages",
                "arguments": arguments or {},
                "messages": [],
                "count": 0,
            },
        }

    @mcp.tool(name="jira.create_issue")
    def jira_create_issue(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {"tool": "jira.create_issue", "arguments": arguments or {}},
        }

    @mcp.tool(name="jira.get_issue")
    def jira_get_issue(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        issue_key = args.get("issue_key", "HELP-1")
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {
                "tool": "jira.get_issue",
                "arguments": args,
                "key": issue_key,
                "summary": f"Mock summary for {issue_key}",
                "description": "Mock description",
                "priority": "medium",
                "comments": [],
            },
        }

    @mcp.tool(name="jira.add_comment")
    def jira_add_comment(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {"tool": "jira.add_comment", "arguments": arguments or {}},
        }

    @mcp.tool(name="jira.update_issue")
    def jira_update_issue(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {"tool": "jira.update_issue", "arguments": arguments or {}},
        }

    @mcp.tool()
    def generic_tool(arguments: dict[str, Any] | None = None) -> dict[str, Any]:  # pragma: no cover - fallback
        return {
            "status": "completed",
            "action_id": f"action-{uuid.uuid4().hex[:8]}",
            "content": {"tool": "generic_tool", "arguments": arguments or {}},
        }

    return mcp


class MockMCPServer:
    """Run the mock MCP server in a background thread over streamable-http."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._mcp = _build_server()
        app = self._mcp.http_app(path="/mcp", transport="streamable-http")
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="error")
        )
        import threading

        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        import threading
        import time

        self._thread.start()
        # Wait until the server is accepting connections
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("Mock MCP server failed to start")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
