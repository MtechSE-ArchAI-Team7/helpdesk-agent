import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from botocore.credentials import Credentials

from helpdesk_agent.config import MCPConfig
from helpdesk_agent.infrastructure.mcp.transport import GatewayIAMAuth, MCPProxyAdapter
from helpdesk_agent.mock_mcp_server import MockMCPServer


@pytest.fixture(scope="module")
def mock_server():
    server = MockMCPServer(port=8765)
    server.start()
    yield server
    server.stop()


@pytest.mark.asyncio
async def test_call_tool(mock_server) -> None:
    adapter = MCPProxyAdapter()
    response = await adapter.call_tool("jira.create_issue", {"summary": "Password reset"})
    assert response.status == "completed"
    assert response.action_id
    assert response.content["tool"] == "jira.create_issue"
    assert response.content["arguments"]["summary"] == "Password reset"


@pytest.mark.asyncio
async def test_async_tool_uses_pending_status(mock_server) -> None:
    adapter = MCPProxyAdapter()
    response = await adapter.call_tool(
        "zohomail.ZohoMail_sendEmail",
        {
            "path_variables": {"accountId": "a1"},
            "body": {"fromAddress": "h@d.c", "toAddress": "a@b.c", "subject": "s", "content": "b"},
        },
        wait_for_completion=False,
    )
    assert response.status == "queued"
    assert response.action_id

    polled = await adapter.get_action_status(response.action_id)
    assert polled.status == "completed"
    assert polled.content["observed"] is True


def test_resolve_url_routes_by_tool_prefix() -> None:
    config = MCPConfig(
        base_url="http://default:1/mcp",
        atlassian_url="http://jira:2/mcp",
        zohomail_url="http://zoho:3/mcp",
        faq_url="http://faq:4/mcp",
        playwright_url="http://playwright:5/mcp",
    )
    adapter = MCPProxyAdapter(config)
    assert adapter._resolve_url("jira.create_issue") == "http://jira:2/mcp"
    assert adapter._resolve_url("jira.add_comment") == "http://jira:2/mcp"
    assert adapter._resolve_url("confluence.search") == "http://jira:2/mcp"
    assert adapter._resolve_url("zohomail.ZohoMail_sendReplyEmail") == "http://zoho:3/mcp"
    assert adapter._resolve_url("faq.search") == "http://faq:4/mcp"
    assert adapter._resolve_url("playwright.browser_navigate") == "http://playwright:5/mcp"
    assert adapter._resolve_url("actions/get") == "http://default:1/mcp"


def test_resolve_url_falls_back_to_base_url() -> None:
    """Without dedicated URLs, all tools use base_url."""
    adapter = MCPProxyAdapter(MCPConfig(base_url="http://default:1/mcp"))
    assert adapter._resolve_url("zohomail.ZohoMail_sendReplyEmail") == "http://default:1/mcp"
    assert adapter._resolve_url("faq.search") == "http://default:1/mcp"
    assert adapter._resolve_url("playwright.browser_navigate") == "http://default:1/mcp"
    # jira.* only routes away when atlassian_url is configured
    assert adapter._resolve_url("jira.create_issue") == "http://default:1/mcp"


def test_translate_add_comment_uses_body_field() -> None:
    """mcp-atlassian jira_add_comment requires 'body', not 'comment'."""
    adapter = MCPProxyAdapter(MCPConfig(atlassian_url="http://jira:2/mcp"))
    name, args = adapter._translate_atlassian_call(
        "jira.add_comment", {"issue_key": "HELP-1", "comment": "Diagnosis required"}
    )
    assert name == "jira_add_comment"
    assert args == {"issue_key": "HELP-1", "body": "Diagnosis required"}


def test_translate_update_issue_serializes_fields_as_json_string() -> None:
    """mcp-atlassian jira_update_issue takes 'fields' as a JSON string."""
    import json

    adapter = MCPProxyAdapter(MCPConfig(atlassian_url="http://jira:2/mcp"))
    name, args = adapter._translate_atlassian_call(
        "jira.update_issue",
        {"issue_key": "HELP-1", "fields": {"summary": "New", "priority": "high"}},
    )
    assert name == "jira_update_issue"
    assert args["issue_key"] == "HELP-1"
    assert isinstance(args["fields"], str)
    parsed = json.loads(args["fields"])
    # priority is passed as a plain name string, not {"name": ...}
    assert parsed == {"summary": "New", "priority": "high"}


def test_translate_zohomail_strips_routing_prefix() -> None:
    """The internal 'zohomail.' prefix is stripped before calling the server."""
    adapter = MCPProxyAdapter(MCPConfig(zohomail_url="http://zoho:3/mcp"))
    assert adapter._translate_zohomail_call("zohomail.ZohoMail_sendReplyEmail") == "ZohoMail_sendReplyEmail"
    assert adapter._translate_zohomail_call("zohomail.ZohoMail_getMailAccounts") == "ZohoMail_getMailAccounts"


def test_translate_playwright_adds_gateway_target_prefix() -> None:
    adapter = MCPProxyAdapter(MCPConfig(playwright_tool_prefix="mcp-atlassian___"))
    assert adapter._translate_playwright_call("playwright.browser_navigate") == "mcp-atlassian___browser_navigate"


@pytest.mark.asyncio
async def test_iam_auth_signs_each_method_with_temporary_credentials() -> None:
    auth = GatewayIAMAuth("ap-southeast-2")
    auth.session = MagicMock()
    auth.session.get_credentials.return_value = Credentials("test-access", "test-secret", "test-session")
    observed = []

    def transport(request):
        observed.append(request)
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "/ap-southeast-2/bedrock-agentcore/aws4_request" in request.headers["Authorization"]
        assert request.headers["X-Amz-Security-Token"] == "test-session"
        assert "X-Amz-Date" in request.headers
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(auth=auth, transport=httpx.MockTransport(transport)) as client:
        for method in ("POST", "GET", "DELETE"):
            await client.request(
                method,
                "https://rag.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp",
                headers={"Authorization": "Bearer test-token"},
                content=b'{"jsonrpc":"2.0"}' if method == "POST" else b"",
            )

    assert [request.method for request in observed] == ["POST", "GET", "DELETE"]
    assert auth.session.get_credentials.call_count == 3


@pytest.mark.asyncio
async def test_playwright_sequence_uses_one_session_and_direct_arguments(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="helpdesk_agent.infrastructure.mcp.transport")
    calls = []
    events = []
    sessions = {"initialized": 0}

    @asynccontextmanager
    async def fake_streamablehttp_client(*args, **kwargs):
        yield object(), object(), None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            sessions["initialized"] += 1

        async def list_tools(self, cursor=None):
            events.append(("tools/list", cursor))
            if cursor is None:
                return SimpleNamespace(tools=[SimpleNamespace(name="browser_navigate")], nextCursor="page-2")
            return SimpleNamespace(tools=[SimpleNamespace(name="browser_snapshot")], nextCursor=None)

        async def call_tool(self, tool_name, arguments):
            events.append((tool_name, None))
            calls.append((tool_name, arguments))
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text='{"status":"completed"}')],
                structuredContent={},
            )

    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.ClientSession", lambda *_: FakeSession())
    adapter = MCPProxyAdapter(MCPConfig(playwright_url="http://playwright:5/mcp", playwright_tool_prefix=""))

    responses = await adapter.call_tool_sequence(
        [
            ("playwright.browser_navigate", {"url": "https://portal.example.com"}),
            ("playwright.browser_snapshot", {}),
        ]
    )

    assert [response.status for response in responses] == ["completed", "completed"]
    assert sessions["initialized"] == 1
    assert events == [
        ("tools/list", None),
        ("tools/list", "page-2"),
        ("browser_navigate", None),
        ("browser_snapshot", None),
    ]
    assert 'count=2 tools=["browser_navigate", "browser_snapshot"]' in caplog.text
    assert "tool=browser_navigate status=started" in caplog.text
    assert calls == [
        ("browser_navigate", {"url": "https://portal.example.com"}),
        ("browser_snapshot", {}),
    ]


@pytest.mark.asyncio
async def test_playwright_sequence_logs_nested_error_and_failing_tool(monkeypatch, caplog) -> None:
    @asynccontextmanager
    async def fake_streamablehttp_client(*args, **kwargs):
        yield object(), object(), None

    class NestedError(Exception):
        exceptions = (RuntimeError("unknown tool: browser_navigate"),)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[], nextCursor=None)

        async def call_tool(self, tool_name, arguments):
            raise NestedError("MCP sequence failed")

    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.ClientSession", lambda *_: FakeSession())
    adapter = MCPProxyAdapter(MCPConfig(playwright_url="http://playwright:5/mcp"))

    responses = await adapter.call_tool_sequence(
        [("playwright.browser_navigate", {"url": "https://portal.example.com"})]
    )

    assert responses[0].status == "failed"
    assert "tool=mcp-playwright___browser_navigate status=failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "unknown tool: browser_navigate" in caplog.text
