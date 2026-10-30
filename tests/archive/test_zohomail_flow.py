import pytest

from helpdesk_agent.infrastructure.mcp.email import ZohoMailFlow
from helpdesk_agent.infrastructure.mcp.transport import MCPProxyAdapter
from helpdesk_agent.mock_mcp_server import MockMCPServer


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
        message_id="123456789",
        body="Reply text",
        subject="[HELP-1] Test Subject",
        to="user@example.com",
    )
    assert result["status"] == "sent"
    assert result["reply_message_id"]


@pytest.mark.asyncio
async def test_reply_email_rfc822_id_falls_back_to_send(mock_server) -> None:
    """RFC822 Message-ID headers can't thread; falls back to a fresh sendEmail."""
    adapter = MCPProxyAdapter()
    flow = ZohoMailFlow(adapter)
    result = await flow.reply_email(
        message_id="<abc@mail.gmail.com>",
        body="Reply text",
        subject="[HELP-1] Test Subject",
        to="user@example.com",
    )
    assert result["status"] == "sent"
    assert result["message_id"]
    assert "not a Zoho internal ID" in result["note"]


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
        message_id="123456789",
        body="Reply text",
        subject="[HELP-1] Test",
    )
    assert result["status"] == "mocked"
    assert result["reply_message_id"]
    assert result["subject"] == "[HELP-1] Test"
