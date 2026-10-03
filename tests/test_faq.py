import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from helpdesk_agent.config import MCPConfig
from helpdesk_agent.mcp import GatewayIAMAuth, MCPProxyAdapter
from helpdesk_agent.mock_mcp_server import MockMCPServer
from helpdesk_agent.modules.faq.service import FAQFlow


@pytest.fixture(scope="module")
def mock_server():
    server = MockMCPServer(port=8765)
    server.start()
    yield server
    server.stop()


@pytest.mark.asyncio
async def test_faq_search_mock_mode() -> None:
    """Test FAQ search in mock mode (no MCP adapter)."""
    flow = FAQFlow()
    result = await flow.search("password reset")
    assert result["status"] == "mocked"
    assert result["count"] == 0
    assert result["results"] == []


@pytest.mark.asyncio
async def test_faq_search_with_mcp(mock_server) -> None:
    """Test FAQ search with MCP adapter."""
    adapter = MCPProxyAdapter()
    flow = FAQFlow(adapter)
    result = await flow.search("password reset", max_results=5)

    assert result["status"] == "success"
    assert result["count"] == 2
    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "How to reset your password"
    assert result["results"][0]["score"] == 0.95


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["bearer", "iam"])
async def test_knowledge_search_sends_prefixed_direct_arguments_and_normalizes_results(monkeypatch, auth_mode) -> None:
    calls = []
    payload = {
        "retrievalResults": [{
            "content": {"type": "TEXT", "text": "Approved troubleshooting steps"},
            "score": 0.85,
            "metadata": {"title": "Checkout errors"},
            "location": {"s3Location": {"uri": "s3://knowledge/runbook.txt"}},
        }, {
            "content": {"type": "IMAGE", "byteContent": "image-data"},
            "score": 0.99,
        }],
    }

    @asynccontextmanager
    async def fake_transport(*args, **kwargs):
        assert args[0] == "https://rag.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp"
        if auth_mode == "iam":
            assert isinstance(kwargs["auth"], GatewayIAMAuth)
            assert kwargs["auth"].region == "ap-southeast-2"
            assert kwargs["headers"] is None
        else:
            assert "auth" not in kwargs
            assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
        yield object(), object(), None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=False, content=[SimpleNamespace(text=json.dumps(payload))], structuredContent={},
            )

    monkeypatch.setattr("helpdesk_agent.mcp.streamablehttp_client", fake_transport)
    monkeypatch.setattr("helpdesk_agent.mcp.ClientSession", lambda *_: FakeSession())
    adapter = MCPProxyAdapter(MCPConfig(
        faq_url="https://rag.gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp",
        faq_tool_prefix="ops-knowledge___",
        faq_auth_mode=auth_mode,
        headers={"Authorization": "Bearer test-token"},
    ))

    result = await FAQFlow(adapter).search("Checkout returns HTTP 500", max_results=3)

    assert calls == [("ops-knowledge___Retrieve", {
        "retrievalQuery": {"text": "Checkout returns HTTP 500"},
    })]
    assert result["count"] == 1
    assert result["results"][0]["body"] == "Approved troubleshooting steps"
    assert result["results"][0]["title"] == "Checkout errors"
    assert result["results"][0]["location"] == payload["retrievalResults"][0]["location"]
    assert result["results"][0]["score"] == 0.85
