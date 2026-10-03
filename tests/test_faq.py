from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from helpdesk_agent.config import MCPConfig
from helpdesk_agent.mcp import MCPProxyAdapter
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
async def test_knowledge_search_sends_prefixed_direct_arguments_and_normalizes_results(monkeypatch) -> None:
    calls = []
    payload = {
        "resultCount": 1,
        "results": [{
            "text": "Approved troubleshooting steps",
            "score": 0.85,
            "metadata": {"title": "Checkout errors"},
            "location": {"s3Location": {"uri": "s3://knowledge/runbook.txt"}},
        }],
    }

    @asynccontextmanager
    async def fake_transport(*args, **kwargs):
        assert args[0] == "https://gateway.example/mcp"
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
            return SimpleNamespace(isError=False, content=[], structuredContent=payload)

    monkeypatch.setattr("helpdesk_agent.mcp.streamablehttp_client", fake_transport)
    monkeypatch.setattr("helpdesk_agent.mcp.ClientSession", lambda *_: FakeSession())
    adapter = MCPProxyAdapter(MCPConfig(
        faq_url="https://gateway.example/mcp", faq_tool_prefix="ops-knowledge___",
    ))

    result = await FAQFlow(adapter).search("Checkout returns HTTP 500", max_results=3)

    assert calls == [("ops-knowledge___search_ops_knowledge", {
        "query": "Checkout returns HTTP 500", "numberOfResults": 3,
    })]
    assert result["count"] == 1
    assert result["results"][0]["body"] == "Approved troubleshooting steps"
    assert result["results"][0]["title"] == "Checkout errors"
    assert result["results"][0]["location"] == payload["results"][0]["location"]
