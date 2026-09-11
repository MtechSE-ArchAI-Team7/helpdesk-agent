import pytest

from helpdesk_agent.mcp import MCPProxyAdapter
from helpdesk_agent.mock_mcp_server import MockMCPServer
from helpdesk_agent.modules.email.service import ZohoMailFlow


@pytest.fixture(scope="module")
def mock_server():
    server = MockMCPServer(port=8765)
    server.start()
    yield server
    server.stop()


@pytest.mark.asyncio
async def test_send_email_returns_message_id(mock_server) -> None:
    adapter = MCPProxyAdapter()
    flow = ZohoMailFlow(adapter)
    result = await flow.send_email(
        to="recipient@example.com",
        subject="Test Subject",
        body="Test body",
    )
    assert result["status"] == "sent"
    assert result["message_id"]
    assert result["timestamp"]


@pytest.mark.asyncio
async def test_reply_email_with_subject_override(mock_server) -> None:
    adapter = MCPProxyAdapter()
    flow = ZohoMailFlow(adapter)
    result = await flow.reply_email(
        message_id="msg-123",
        body="Reply text",
        subject="[HELP-1] Test Subject",
    )
    assert result["status"] == "sent"
    assert result["reply_message_id"]


@pytest.mark.asyncio
async def test_get_messages(mock_server) -> None:
    adapter = MCPProxyAdapter()
    flow = ZohoMailFlow(adapter)
    result = await flow.get_messages(query="is:unread", max_results=10)
    assert result["status"] == "success"
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_send_email_mock_mode() -> None:
    flow = ZohoMailFlow()
    result = await flow.send_email(
        to="recipient@example.com",
        subject="Test Subject",
        body="Test body",
    )
    assert result["status"] == "mocked"
    assert result["message_id"]


@pytest.mark.asyncio
async def test_get_messages_mock_mode() -> None:
    flow = ZohoMailFlow()
    result = await flow.get_messages(query="is:unread", max_results=10)
    assert result["status"] == "mocked"
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_reply_email_mock_mode() -> None:
    flow = ZohoMailFlow()
    result = await flow.reply_email(
        message_id="msg-123",
        body="Reply text",
        subject="[HELP-1] Test",
    )
    assert result["status"] == "mocked"
    assert result["reply_message_id"]
    assert result["subject"] == "[HELP-1] Test"
