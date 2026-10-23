from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from helpdesk_agent.config import MCPConfig
from helpdesk_agent.domain.policies.browser import BrowserTargetPolicy
from helpdesk_agent.infrastructure.mcp.transport import MCPProxyAdapter


class PlaywrightValidationFlow(BrowserTargetPolicy):
    """Run read-only browser diagnostics through the hosted Playwright MCP server."""

    _MAX_EVIDENCE_CHARS = 12000

    def __init__(
        self,
        mcp_adapter: MCPProxyAdapter | None = None,
        config: MCPConfig | None = None,
    ) -> None:
        self.mcp_adapter = mcp_adapter
        self.config = config or (mcp_adapter.config if mcp_adapter else MCPConfig.from_env())

    @property
    def allowed_hosts(self) -> list[str]:
        return self.config.playwright_allowed_hosts

    async def validate(self, target_url: str) -> dict[str, Any]:
        """Navigate, inspect page state, and collect browser diagnostics in one session."""
        if self.mcp_adapter is None:
            return {"status": "failed", "tool_statuses": [], "evidence": "", "error": "mcp_disabled"}

        calls: list[tuple[str, dict[str, Any]]] = [
            ("playwright.browser_navigate", {"url": target_url}),
            ("playwright.browser_snapshot", {}),
            ("playwright.browser_console_messages", {"level": "error"}),
            ("playwright.browser_network_requests", {"static": False}),
        ]
        responses = await self.mcp_adapter.call_tool_sequence(
            calls,
            timeout_seconds=self.config.playwright_timeout_seconds,
        )

        evidence_parts: list[str] = []
        tool_statuses: list[dict[str, str]] = []
        for (tool_name, _), response in zip(calls, responses, strict=False):
            tool = tool_name.removeprefix("playwright.")
            tool_statuses.append({"tool": tool, "status": response.status})
            if response.content:
                evidence_parts.append(f"[{tool}] {json.dumps(response.content, default=str)}")
            if response.error:
                evidence_parts.append(f"[{tool} error] {response.error}")

        evidence = "\n".join(evidence_parts)[: self._MAX_EVIDENCE_CHARS]
        failed = len(responses) != len(calls) or any(response.status != "completed" for response in responses)
        return {
            "status": "failed" if failed else "completed",
            "tool_statuses": tool_statuses,
            "evidence": evidence,
            "error": next((response.error for response in responses if response.error), None),
        }

    @asynccontextmanager
    async def open_session(self, target_url: str) -> AsyncIterator[PlaywrightBrowserSession]:
        if self.mcp_adapter is None:
            raise RuntimeError("MCP is disabled; browser session cannot be opened")
        async with self.mcp_adapter.open_playwright_session() as session:
            yield PlaywrightBrowserSession(session, target_url)


class PlaywrightBrowserSession:
    """Enforce the approved target and read-only inspection allowlist."""

    _INSPECTION_ARGUMENTS: dict[str, dict[str, Any]] = {
        "browser_snapshot": {},
        "browser_console_messages": {"level": "error"},
        "browser_network_requests": {"static": False},
    }

    def __init__(self, session: Any, target_url: str) -> None:
        self._session = session
        self._target_url = target_url
        self._navigated = False

    async def navigate(self) -> dict[str, Any]:
        if self._navigated:
            raise ValueError("Browser navigation is permitted only once per validation run")
        self._navigated = True
        return await self._call("browser_navigate", {"url": self._target_url})

    async def inspect(self, tool: str) -> dict[str, Any]:
        if tool not in self._INSPECTION_ARGUMENTS:
            raise ValueError(f"Browser inspection tool is not permitted: {tool}")
        return await self._call(tool, self._INSPECTION_ARGUMENTS[tool])

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self._session.call_tool(f"playwright.{tool}", arguments)
        return {
            "tool": tool,
            "status": response.status,
            "content": response.content,
            "error": response.error,
        }
