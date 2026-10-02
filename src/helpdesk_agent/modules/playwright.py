from __future__ import annotations

import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ..config import MCPConfig
from ..mcp import MCPProxyAdapter


class PlaywrightAssessment(BaseModel):
    decision: Literal["eligible", "needs_info", "not_applicable"]
    reason: str
    target_url: str | None = None
    expected_error: str = ""
    clarifying_questions: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)


class PlaywrightEvaluation(BaseModel):
    status: Literal["reproduced", "not_reproduced", "inconclusive"]
    summary: str


class PlaywrightValidationFlow:
    """Run read-only browser diagnostics through the hosted Playwright MCP server."""

    _URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
    _TRAILING_URL_PUNCTUATION = ".,;:!?)]}"
    _MAX_EVIDENCE_CHARS = 12000

    def __init__(
        self,
        mcp_adapter: MCPProxyAdapter | None = None,
        config: MCPConfig | None = None,
    ) -> None:
        self.mcp_adapter = mcp_adapter
        self.config = config or (mcp_adapter.config if mcp_adapter else MCPConfig.from_env())

    @classmethod
    def reported_urls(cls, report: str) -> list[str]:
        urls = []
        for match in cls._URL_PATTERN.findall(report):
            url = match.rstrip(cls._TRAILING_URL_PUNCTUATION)
            if url and url not in urls:
                urls.append(url)
        return urls

    def validate_target(self, target_url: str | None, report: str) -> tuple[str | None, str]:
        """Return the normalized allowed host, or a reason why navigation is denied."""
        if not target_url:
            return None, "no_target_url"
        if target_url not in self.reported_urls(report):
            return None, "target_url_not_in_user_report"

        try:
            parsed = urlsplit(target_url)
            host = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except ValueError:
            return None, "invalid_target_url"

        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return None, "https_url_required"
        if port not in (None, 443):
            return None, "nonstandard_port_not_allowed"

        allowed_hosts = {host.lower().rstrip(".") for host in self.config.playwright_allowed_hosts}
        if host not in allowed_hosts:
            return None, "host_not_allowlisted"
        return host, "allowed"

    async def validate(self, target_url: str) -> dict[str, Any]:
        """Navigate, inspect page state, and collect browser diagnostics in one session."""
        if self.mcp_adapter is None:
            return {"status": "failed", "tool_statuses": [], "evidence": "", "error": "mcp_disabled"}

        calls = [
            ("playwright.browser_navigate", {"url": target_url}),
            ("playwright.browser_snapshot", {}),
            ("playwright.browser_console_messages", {"level": "error"}),
            ("playwright.browser_network_requests", {}),
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