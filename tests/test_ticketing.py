import pytest

from helpdesk_agent.mcp import MCPProxyAdapter
from helpdesk_agent.mock_mcp_server import MockMCPServer
from helpdesk_agent.modules.ticketing.service import JiraTicketFlow, to_jira_priority


def test_to_jira_priority_mapping() -> None:
    assert to_jira_priority("critical") == "Highest"
    assert to_jira_priority("high") == "High"
    assert to_jira_priority("medium") == "Medium"
    assert to_jira_priority("low") == "Low"
    assert to_jira_priority(" High ") == "High"
    # Unknown values pass through title-cased (custom priority schemes)
    assert to_jira_priority("urgent") == "Urgent"


@pytest.fixture(scope="module")
def mock_server():
    server = MockMCPServer(port=8765)
    server.start()
    yield server
    server.stop()


@pytest.mark.asyncio
async def test_create_ticket_returns_ticket_info(mock_server) -> None:
    adapter = MCPProxyAdapter()
    flow = JiraTicketFlow(adapter)
    result = await flow.create_ticket(
        "case-010",
        "User cannot log in",
        "The user reports an inability to authenticate into the portal.",
        "identity",
        "high",
        "alice@example.com",
    )
    assert result["status"] == "created"
    assert result["ticket_id"].startswith("case-010") or result["ticket_id"].startswith("JIRA-")
    assert result["ticket_url"].startswith("https://jira.example.com/browse/")


@pytest.mark.asyncio
async def test_create_ticket_mock_mode() -> None:
    flow = JiraTicketFlow()
    result = await flow.create_ticket(
        "case-011",
        "Service outage detected",
        "The service is down in production.",
        "platform",
        "critical",
        "bob@example.com",
    )
    assert result["status"] == "mocked"
    assert result["ticket_id"] == "JIRA-case-011"


@pytest.mark.asyncio
async def test_get_ticket(mock_server) -> None:
    adapter = MCPProxyAdapter()
    flow = JiraTicketFlow(adapter)
    result = await flow.get_ticket("HELP-123")
    assert result["status"] == "success"
    assert result["ticket_id"] == "HELP-123"
    assert "summary" in result
    assert "comments" in result


@pytest.mark.asyncio
async def test_get_ticket_mock_mode() -> None:
    flow = JiraTicketFlow()
    result = await flow.get_ticket("HELP-123")
    assert result["status"] == "mocked"
    assert result["ticket_id"] == "HELP-123"
    assert result["comments"] == []


@pytest.mark.asyncio
async def test_get_ticket_atlassian_payload_with_workflow_status() -> None:
    """Regression: mcp-atlassian's jira_get_issue returns the issue's own
    workflow status ({"name": "To Do", ...}) at the payload's top level.
    That field must not be mistaken for a Zoho-style status envelope, or the
    lookup is misreported as failed and the workflow creates duplicate tickets.
    """
    import json
    from unittest.mock import patch

    from helpdesk_agent.config import MCPConfig

    payload = {
        "id": "10019",
        "key": "AH-19",
        "summary": "Login issue",
        "description": "User cannot log in",
        "status": {"name": "To Do", "category": "To Do", "color": "blue-gray"},
        "priority": {"name": "Medium"},
        "comments": [],
    }

    class _FakeText:
        def __init__(self, text: str) -> None:
            self.text = text

    class _FakeResult:
        isError = False
        structuredContent = None
        content = [_FakeText(json.dumps(payload))]

    class _FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, name, args):
            assert name == "jira_get_issue"
            assert args == {"issue_key": "AH-19"}
            return _FakeResult()

    class _FakeHTTP:
        async def __aenter__(self):
            return (None, None, None)

        async def __aexit__(self, *args):
            return False

    adapter = MCPProxyAdapter(MCPConfig(atlassian_url="http://fake:8000/mcp", jira_project_key="AH"))
    with (
        patch("helpdesk_agent.mcp.streamablehttp_client", lambda *a, **k: _FakeHTTP()),
        patch("helpdesk_agent.mcp.ClientSession", _FakeSession),
    ):
        flow = JiraTicketFlow(adapter)
        result = await flow.get_ticket("AH-19")

    assert result["status"] == "success"
    assert result["ticket_id"] == "AH-19"
    assert result["summary"] == "Login issue"


@pytest.mark.asyncio
async def test_add_comment(mock_server) -> None:
    adapter = MCPProxyAdapter()
    flow = JiraTicketFlow(adapter)
    result = await flow.add_comment("HELP-123", "Diagnosis required")
    assert result["status"] == "commented"
    assert result["ticket_id"] == "HELP-123"


@pytest.mark.asyncio
async def test_add_comment_mock_mode() -> None:
    flow = JiraTicketFlow()
    result = await flow.add_comment("HELP-123", "Diagnosis required")
    assert result["status"] == "mocked"
    assert result["comment"] == "Diagnosis required"


@pytest.mark.asyncio
async def test_update_ticket(mock_server) -> None:
    adapter = MCPProxyAdapter()
    flow = JiraTicketFlow(adapter)
    result = await flow.update_ticket("HELP-123", summary="New summary", priority="high")
    assert result["status"] == "updated"
    assert result["ticket_id"] == "HELP-123"
    assert set(result["updated"]) == {"summary", "priority"}


@pytest.mark.asyncio
async def test_update_ticket_mock_mode() -> None:
    flow = JiraTicketFlow()
    result = await flow.update_ticket("HELP-123", summary="New summary", description="New desc")
    assert result["status"] == "mocked"
    assert result["updated"] == {"summary": "New summary", "description": "New desc"}
