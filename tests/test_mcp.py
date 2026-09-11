import pytest

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
    response = await adapter.call_tool("zohomail.send_email", {"to": "a@b.c", "subject": "s", "body": "b"}, wait_for_completion=False)
    assert response.status == "queued"
    assert response.action_id

    polled = await adapter.get_action_status(response.action_id)
    assert polled.status == "completed"
    assert polled.content["observed"] is True
