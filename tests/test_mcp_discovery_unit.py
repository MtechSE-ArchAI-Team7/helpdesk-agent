import secrets
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.types import Tool

from helpdesk_agent.config import MCPConfig
from helpdesk_agent.mcp import GatewayIAMAuth
from tests.integration import test_mcp as discovery

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("service", discovery.SERVICES)
def test_discovery_requires_explicit_endpoint(monkeypatch, service):
    monkeypatch.delenv("HELPDESK_AGENT_MCP_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="explicitly set"):
        discovery._endpoint(service, MCPConfig())


def test_explicit_base_endpoint_fallback(monkeypatch):
    monkeypatch.setenv("HELPDESK_AGENT_MCP_BASE_URL", "http://localhost:8765/mcp")
    assert discovery._endpoint("faq", MCPConfig()) == "http://localhost:8765/mcp"


@pytest.mark.parametrize("prefix,expected", [(None, "faq.search"), ("", "Retrieve"), ("kb___", "kb___Retrieve")])
def test_faq_discovery_names(prefix, expected):
    assert discovery._expected_tools("faq", MCPConfig(faq_tool_prefix=prefix)) == {expected}


def test_configured_tool_prefixes():
    config = MCPConfig(atlassian_tool_prefix="jira___", zohomail_tool_prefix="mail_", playwright_tool_prefix="")
    assert "jira___jira_create_issue" in discovery._expected_tools("atlassian", config)
    assert "mail_sendEmail" in discovery._expected_tools("zohomail", config)
    assert "browser_navigate" in discovery._expected_tools("playwright", config)


def test_iam_region_inference_and_override():
    config = MCPConfig(faq_auth_mode="iam")
    auth = discovery._iam_auth(
        "faq", config, "https://example.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com/mcp"
    )
    assert isinstance(auth, GatewayIAMAuth)
    assert auth.region == "ap-southeast-1"
    config.faq_region = "us-west-2"
    assert discovery._iam_auth("faq", config, "https://custom.example/mcp").region == "us-west-2"


def test_invalid_iam_configuration():
    with pytest.raises(ValueError, match="FAQ_REGION"):
        discovery._iam_auth("faq", MCPConfig(faq_auth_mode="iam"), "https://custom.example/mcp")
    with pytest.raises(ValueError, match="bearer or iam"):
        discovery._iam_auth("faq", MCPConfig(faq_auth_mode="invalid"), "https://custom.example/mcp")


@pytest.mark.parametrize("iam", [False, True])
@pytest.mark.asyncio
async def test_discovery_initializes_and_lists_all_pages_without_tool_calls(monkeypatch, iam):
    tools = [
        Tool(name="one", inputSchema={"type": "object"}),
        Tool(name="two", inputSchema={"type": "object"}),
    ]
    session = AsyncMock()
    session.initialize.return_value = SimpleNamespace(capabilities=SimpleNamespace(tools=object()))
    session.list_tools.side_effect = [
        SimpleNamespace(tools=tools[:1], nextCursor="page-2"),
        SimpleNamespace(tools=tools[1:], nextCursor=None),
    ]
    options = {}

    @asynccontextmanager
    async def transport(url, **kwargs):
        options.update(kwargs)
        yield None, None, None

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield session

    monkeypatch.setattr(discovery, "streamablehttp_client", transport)
    monkeypatch.setattr(discovery, "ClientSession", client)
    value = secrets.token_urlsafe(24)
    config = MCPConfig(
        faq_url="https://custom.example/mcp",
        auth_token=value,
        headers={"Authorization": f"Bearer {value}", "X-Test-Header": "preserved"},
        faq_auth_mode="iam" if iam else "bearer",
        faq_region="us-west-2",
    )
    assert await discovery._discover("faq", config) == tools
    session.initialize.assert_awaited_once()
    assert session.list_tools.await_args_list[0].kwargs == {"cursor": None}
    assert session.list_tools.await_args_list[1].kwargs == {"cursor": "page-2"}
    session.call_tool.assert_not_called()
    expected_headers = {"X-Test-Header": "preserved"}
    if not iam:
        expected_headers["Authorization"] = f"Bearer {value}"
    assert options["headers"] == expected_headers
    assert isinstance(options["auth"], GatewayIAMAuth) is iam
    assert options["sse_read_timeout"] == config.timeout_seconds


@pytest.mark.asyncio
async def test_zoho_initialization_failure_never_lists_or_calls_tools(monkeypatch):
    session = AsyncMock()
    session.initialize.side_effect = RuntimeError("Initialization rejected")
    options = {}

    @asynccontextmanager
    async def transport(url, **kwargs):
        options.update(kwargs)
        assert url == "https://zoho.example/mcp"
        yield None, None, None

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield session

    monkeypatch.setattr(discovery, "streamablehttp_client", transport)
    monkeypatch.setattr(discovery, "ClientSession", client)
    config = MCPConfig(zohomail_url="https://zoho.example/mcp", headers={"X-Test": "configured"})
    with pytest.raises(RuntimeError, match="Initialization rejected"):
        await discovery._discover("zohomail", config)
    assert options["headers"] == config.headers
    session.list_tools.assert_not_called()
    session.call_tool.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["Invalid API Key.", "Sensitive server details must not be shown"])
async def test_discovery_http_error_is_actionable_without_raw_response(body):
    response = httpx.Response(400, text=body)
    with pytest.raises(AssertionError, match="MCP discovery HTTP 400") as error:
        await discovery._check_http_response(response)
    assert body not in str(error.value)
    if body == "Invalid API Key.":
        assert "server rejected the API key" in str(error.value)


@pytest.mark.asyncio
async def test_discovery_http_success_is_unchanged():
    await discovery._check_http_response(httpx.Response(200, json={"result": {}}))


@pytest.mark.asyncio
async def test_discovery_rejects_repeated_cursor(monkeypatch):
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(tools=[], nextCursor="repeated")

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield None, None, None

    @asynccontextmanager
    async def client(*args, **kwargs):
        yield session

    monkeypatch.setattr(discovery, "streamablehttp_client", transport)
    monkeypatch.setattr(discovery, "ClientSession", client)
    with pytest.raises(AssertionError, match="repeated pagination cursor"):
        await discovery._discover("faq", MCPConfig(faq_url="http://localhost:8765/mcp"))
    session.call_tool.assert_not_called()


def test_discovery_validates_subset_and_schema():
    tool = Tool(name="faq.search", inputSchema={"type": "object", "properties": {}})
    extra = Tool(name="extra", inputSchema={})
    discovery._validate_tools("faq", [tool, extra], {"faq.search"})
    with pytest.raises(AssertionError, match="missing expected tools"):
        discovery._validate_tools("faq", [extra], {"faq.search"})
    with pytest.raises(AssertionError, match="duplicate"):
        discovery._validate_tools("faq", [tool, tool], {"faq.search"})
    with pytest.raises(AssertionError, match="type object"):
        discovery._validate_tools("faq", [extra], {"extra"})
