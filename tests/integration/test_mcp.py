from __future__ import annotations

import math
import os
import re
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool

from helpdesk_agent.config import MCPConfig
from helpdesk_agent.mcp import GatewayIAMAuth

SERVICES = ("atlassian", "zohomail", "faq", "playwright")


async def _check_http_response(response: httpx.Response) -> None:
    if response.is_error:
        await response.aread()
        detail = (
            "server rejected the API key; verify the configured MCP URL and authentication"
            if response.text.strip() == "Invalid API Key."
            else "server rejected the MCP request; verify endpoint, authentication, and transport"
        )
        raise AssertionError(f"MCP discovery HTTP {response.status_code}: {detail}")


def _http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        auth=auth,
        follow_redirects=True,
        event_hooks={"response": [_check_http_response]},
    )


def _expected_tools(service: str, config: MCPConfig) -> set[str]:
    if service == "atlassian":
        return {
            f"{config.atlassian_tool_prefix}jira_{name}"
            for name in ("create_issue", "get_issue", "update_issue", "add_comment")
        }
    if service == "zohomail":
        return {
            f"{config.zohomail_tool_prefix}{name}"
            for name in ("getMailAccounts", "listEmails", "sendEmail", "sendReplyEmail")
        }
    if service == "faq":
        return {"faq.search" if config.faq_tool_prefix is None else f"{config.faq_tool_prefix}Retrieve"}
    if service == "playwright":
        return {
            f"{config.playwright_tool_prefix}browser_{name}"
            for name in ("navigate", "snapshot", "console_messages", "network_requests")
        }
    raise ValueError(f"Unknown MCP service: {service}")


def _endpoint(service: str, config: MCPConfig) -> str:
    variable = f"HELPDESK_AGENT_MCP_{service.upper()}_URL"
    url = getattr(config, f"{service}_url") or os.getenv("HELPDESK_AGENT_MCP_BASE_URL")
    if not url:
        raise ValueError(f"Set {variable} or explicitly set HELPDESK_AGENT_MCP_BASE_URL for {service}")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{variable} must be an HTTP(S) MCP URL without embedded credentials")
    return url


def _iam_auth(service: str, config: MCPConfig, url: str) -> GatewayIAMAuth | None:
    if service != "faq":
        return None
    if config.faq_auth_mode not in {"bearer", "iam"}:
        raise ValueError("HELPDESK_AGENT_MCP_FAQ_AUTH_MODE must be bearer or iam")
    if config.faq_auth_mode == "bearer":
        return None
    region = config.faq_region
    if not region:
        match = re.search(
            r"\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$",
            urlsplit(url).hostname or "",
        )
        if not match:
            raise ValueError("Set HELPDESK_AGENT_MCP_FAQ_REGION for an IAM endpoint without an AWS region")
        region = match.group(1)
    return GatewayIAMAuth(region)


async def _discover(service: str, config: MCPConfig) -> list[Tool]:
    url = _endpoint(service, config)
    timeout = config.playwright_timeout_seconds if service == "playwright" else config.timeout_seconds
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MCP discovery timeout must be finite and greater than zero")
    auth = _iam_auth(service, config, url)
    headers = dict(config.headers)
    if auth is not None:
        headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
    async with streamablehttp_client(
        url,
        headers=headers or None,
        auth=auth,
        timeout=timeout,
        sse_read_timeout=timeout,
        httpx_client_factory=_http_client,
    ) as (read_stream, write_stream, _):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=timeout),
        ) as session:
            initialized = await session.initialize()
            assert initialized.capabilities.tools is not None, f"{service}: server does not advertise tools"
            tools: list[Tool] = []
            cursor = None
            seen_cursors: set[str] = set()
            for _ in range(100):
                result = await session.list_tools(cursor=cursor)
                tools.extend(result.tools)
                cursor = result.nextCursor
                if cursor is None:
                    return tools
                assert cursor not in seen_cursors, f"{service}: tools/list returned a repeated pagination cursor"
                seen_cursors.add(cursor)
            raise AssertionError(f"{service}: tools/list exceeded 100 pages")


def _validate_tools(service: str, tools: list[Tool], expected: set[str]) -> None:
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)), f"{service}: duplicate advertised tool names"
    missing = expected - set(names)
    assert not missing, f"{service}: missing expected tools: {', '.join(sorted(missing))}"
    for tool in tools:
        if tool.name not in expected:
            continue
        schema = tool.inputSchema
        assert schema.get("type") == "object", f"{service}/{tool.name}: inputSchema must have type object"
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        assert isinstance(properties, dict), f"{service}/{tool.name}: properties must be an object"
        assert all(isinstance(value, (dict, bool)) for value in properties.values()), (
            f"{service}/{tool.name}: each property must be a JSON Schema object or boolean"
        )
        assert isinstance(required, list) and all(isinstance(name, str) for name in required), (
            f"{service}/{tool.name}: required must be a list of strings"
        )


@pytest.mark.integration
@pytest.mark.live_mcp
@pytest.mark.asyncio
@pytest.mark.parametrize("service", SERVICES)
async def test_external_mcp(service: str) -> None:
    config = MCPConfig.from_env()
    tools = await _discover(service, config)
    _validate_tools(service, tools, _expected_tools(service, config))
