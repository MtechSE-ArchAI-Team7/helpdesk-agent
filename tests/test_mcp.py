import pytest

from helpdesk_agent.config import MCPConfig
from helpdesk_agent.mcp import MCPProxyAdapter
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
    response = await adapter.call_tool("zohomail.sendEmail", {"toAddress": "a@b.c", "subject": "s", "content": "b"}, wait_for_completion=False)
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
    )
    adapter = MCPProxyAdapter(config)
    assert adapter._resolve_url("jira.create_issue") == "http://jira:2/mcp"
    assert adapter._resolve_url("jira.add_comment") == "http://jira:2/mcp"
    assert adapter._resolve_url("confluence.search") == "http://jira:2/mcp"
    assert adapter._resolve_url("zohomail.sendReplyEmail") == "http://zoho:3/mcp"
    assert adapter._resolve_url("faq.search") == "http://faq:4/mcp"
    assert adapter._resolve_url("actions/get") == "http://default:1/mcp"


def test_resolve_url_falls_back_to_base_url() -> None:
    """Without dedicated URLs, all tools use base_url."""
    adapter = MCPProxyAdapter(MCPConfig(base_url="http://default:1/mcp"))
    assert adapter._resolve_url("zohomail.sendReplyEmail") == "http://default:1/mcp"
    assert adapter._resolve_url("faq.search") == "http://default:1/mcp"
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
