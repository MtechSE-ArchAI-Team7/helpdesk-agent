from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .config import MCPConfig

logger = logging.getLogger(__name__)


def _unwrap_error(exc: BaseException) -> str:
    """Unwrap ExceptionGroup (e.g. anyio TaskGroup failures) to the root cause."""
    while hasattr(exc, "exceptions") and getattr(exc, "exceptions"):
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"


@dataclass
class MCPResponse:
    action_id: str | None
    status: str
    content: dict[str, Any]
    error: str | None = None


def _content_to_dict(content: list[Any]) -> dict[str, Any]:
    """Convert MCP content blocks into a plain dict.

    The helpdesk flows expect dict-shaped payloads. TextContent blocks that
    contain JSON are parsed; anything else is stringified under "text".
    """
    if not content:
        return {}

    # Single text block is the common case for tool results
    first = content[0]
    text = getattr(first, "text", None)
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            return {"result": parsed}
        except json.JSONDecodeError:
            return {"text": text}

    # Fallback: serialize whatever we got
    return {"content": [getattr(block, "model_dump", lambda: str(block))() for block in content]}


class MCPProxyAdapter:
    """Adapter that speaks MCP streamable-http via the official SDK.

    Routes tools to the appropriate server:
    - jira.* / confluence.* -> the real mcp-atlassian server (atlassian_url),
      with tool names and arguments translated to its schema.
    - zohomail.* -> the Zoho Mail MCP server (zohomail_url).
    - faq.* -> the FAQ MCP server (faq_url).
    - everything else -> the default server (base_url).
    Servers without a dedicated URL fall back to base_url.
    """

    # Tools that belong to the real mcp-atlassian server.
    _ATLASSIAN_PREFIXES = ("jira.", "confluence.")
    _ZOHOMAIL_PREFIX = "zohomail."
    _FAQ_PREFIX = "faq."

    def __init__(self, config: MCPConfig | None = None) -> None:
        self.config = config or MCPConfig()

    def _resolve_url(self, tool_name: str) -> str:
        """Pick the MCP server URL for a tool based on its prefix."""
        if self.config.atlassian_url is not None and tool_name.startswith(self._ATLASSIAN_PREFIXES):
            return self.config.atlassian_url
        if self.config.zohomail_url is not None and tool_name.startswith(self._ZOHOMAIL_PREFIX):
            return self.config.zohomail_url
        if self.config.faq_url is not None and tool_name.startswith(self._FAQ_PREFIX):
            return self.config.faq_url
        return self.config.base_url

    def _is_atlassian_tool(self, tool_name: str) -> bool:
        return self.config.atlassian_url is not None and tool_name.startswith(self._ATLASSIAN_PREFIXES)

    def _is_zohomail_tool(self, tool_name: str) -> bool:
        return tool_name.startswith(self._ZOHOMAIL_PREFIX)

    def _translate_zohomail_call(self, tool_name: str) -> str:
        """Strip the internal 'zohomail.' routing prefix; the remainder is the
        real tool name on the Zoho server (e.g. 'ZohoMail_sendReplyEmail')."""
        return tool_name.removeprefix(self._ZOHOMAIL_PREFIX)

    def _translate_atlassian_call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Map legacy dotted tool names + args onto mcp-atlassian's schema."""
        prefix = self.config.atlassian_tool_prefix
        if tool_name == "jira.create_issue":
            description = arguments.get("description", "")
            reporter = arguments.get("reporter")
            if reporter:
                description = f"{description}\n\nReporter: {reporter}"
            return f"{prefix}jira_create_issue", {
                "project_key": self.config.jira_project_key,
                "summary": arguments.get("summary", "Helpdesk request"),
                "issue_type": "Task",
                "description": description,
            }
        if tool_name == "jira.get_issue":
            return f"{prefix}jira_get_issue", {
                "issue_key": arguments.get("issue_key", ""),
            }
        if tool_name == "jira.add_comment":
            return f"{prefix}jira_add_comment", {
                "issue_key": arguments.get("issue_key", ""),
                "body": arguments.get("comment", ""),
            }
        if tool_name == "jira.update_issue":
            # mcp-atlassian's jira_update_issue takes `fields` as a JSON string,
            # and priority as a plain name string (e.g. "High").
            issue_key = arguments.get("issue_key", "")
            fields = dict(arguments.get("fields") or {})
            return f"{prefix}jira_update_issue", {
                "issue_key": issue_key,
                "fields": json.dumps(fields),
            }
        # Generic fallback: convert dots to underscores, pass args through
        return f"{prefix}{tool_name.replace('.', '_')}", arguments

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        policy: dict[str, Any] | None = None,
        wait_for_completion: bool = True,
    ) -> MCPResponse:
        base_arguments = dict(arguments or {})

        if self._is_atlassian_tool(tool_name):
            url = self.config.atlassian_url or self.config.base_url
            tool_name, tool_arguments = self._translate_atlassian_call(tool_name, base_arguments)
        elif self._is_zohomail_tool(tool_name):
            url = self._resolve_url(tool_name)
            tool_name = self._translate_zohomail_call(tool_name)
            # Zoho's server validates strictly (no additionalProperties), so send
            # the arguments as-is, without the legacy envelope keys.
            tool_arguments = base_arguments
        else:
            url = self._resolve_url(tool_name)
            # Preserve the legacy extra params the mock/proxy servers expect by
            # nesting them under "arguments".
            base_arguments.setdefault("correlation_id", correlation_id or str(uuid4()))
            if idempotency_key is not None:
                base_arguments.setdefault("idempotency_key", idempotency_key)
            if policy:
                base_arguments.setdefault("policy", policy)
            base_arguments.setdefault("wait_for_completion", wait_for_completion)
            tool_arguments = {"arguments": base_arguments}

        try:
            async with streamablehttp_client(
                url,
                headers=self.config.headers or None,
                timeout=self.config.timeout_seconds,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, tool_arguments)
        except Exception as exc:
            error = _unwrap_error(exc)
            logger.error("mcp tool=%s url=%s call failed: %s", tool_name, url, error)
            return MCPResponse(action_id=None, status="failed", content={}, error=error)

        if result.isError:
            error_text = "; ".join(
                getattr(block, "text", str(block)) for block in result.content
            )
            logger.error("mcp tool=%s url=%s returned error: %s", tool_name, url, error_text)
            return MCPResponse(action_id=None, status="failed", content={}, error=error_text)

        content = _content_to_dict(result.content)
        structured = getattr(result, "structuredContent", None) or {}
        data = {**content, **structured} if isinstance(structured, dict) else content

        # Legacy async-polling shim: the old proxy returned "queued" when
        # wait_for_completion was false or the tool was an approval request.
        status = data.get("status", "completed")
        if not wait_for_completion or tool_name.endswith("approval_request"):
            if status == "completed":
                status = "queued"

        return MCPResponse(
            action_id=data.get("action_id"),
            status=status,
            content=data.get("content", data),
            error=data.get("error"),
        )

    async def get_action_status(self, action_id: str) -> MCPResponse:
        """Poll action status. Not a standard MCP method; kept for the mock
        server by calling it as a tool if available, otherwise returns a
        completed placeholder."""
        try:
            async with streamablehttp_client(
                self.config.base_url,
                headers=self.config.headers or None,
                timeout=self.config.timeout_seconds,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool("actions/get", {"action_id": action_id})
        except Exception as exc:
            return MCPResponse(action_id=action_id, status="failed", content={}, error=str(exc))

        if result.isError:
            error_text = "; ".join(
                getattr(block, "text", str(block)) for block in result.content
            )
            return MCPResponse(action_id=action_id, status="failed", content={}, error=error_text)

        data = _content_to_dict(result.content)
        return MCPResponse(
            action_id=data.get("action_id", action_id),
            status=data.get("status", "completed"),
            content=data.get("content", data),
            error=data.get("error"),
        )
