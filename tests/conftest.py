"""Pytest configuration and fixtures for helpdesk agent tests."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Set a dummy API key for tests
os.environ["HELPDESK_AGENT_LLM_API_KEY"] = "test-api-key"
# Default to MCP disabled so webhook/chat tests get mocked flows unless a
# test explicitly enables it (e.g. test_mcp.py sets it before importing).
os.environ.setdefault("HELPDESK_AGENT_USE_MCP", "0")
os.environ["HELPDESK_AGENT_MEMORY_ENABLED"] = "0"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    integration_modules = {
        "test_a2a.py",
        "test_chat_endpoint.py",
        "test_email_read_chat.py",
        "test_email_webhook.py",
        "test_main.py",
        "test_mcp.py",
        "test_memory_webhook.py",
        "test_workflow.py",
        "test_zohomail_flow.py",
    }
    unit_modules = {
        "test_agentcore_deploy.py",
        "test_chat.py",
        "test_config.py",
        "test_faq.py",
        "test_memory.py",
        "test_ticketing.py",
    }
    for item in items:
        categories = [name for name in ("unit", "integration") if item.get_closest_marker(name)]
        if len(categories) > 1:
            raise pytest.UsageError(f"Test has multiple categories: {item.nodeid}")
        if categories:
            continue
        filename = Path(str(item.path)).name
        if filename in integration_modules:
            item.add_marker(pytest.mark.integration)
        elif filename in unit_modules:
            item.add_marker(pytest.mark.unit)
        else:
            raise pytest.UsageError(f"Assign a unit or integration marker to {item.nodeid}")


@pytest.fixture(autouse=True)
def _reset_dependency_caches():
    """Clear lru_cached dependencies so env/config changes are picked up."""
    from helpdesk_agent import dependencies

    dependencies.get_mcp_config.cache_clear()
    dependencies.get_mcp_adapter.cache_clear()
    dependencies.get_a2a_config.cache_clear()
    dependencies.get_a2a_client_manager.cache_clear()
    dependencies.get_memory.cache_clear()
    yield
    dependencies.get_mcp_config.cache_clear()
    dependencies.get_mcp_adapter.cache_clear()
    dependencies.get_a2a_config.cache_clear()
    dependencies.get_a2a_client_manager.cache_clear()
    dependencies.get_memory.cache_clear()


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client for testing."""
    from helpdesk_agent.modules.triage.llm import LLMTriageClient
    from helpdesk_agent.modules.triage.schema import TriageResult

    client = MagicMock(spec=LLMTriageClient)
    client.triage_email = AsyncMock(
        return_value=TriageResult(
            service="identity",
            issue_type="access",
            priority="high",
            confidence=0.9,
            missing_info=[],
            clarifying_questions=[],
            summary="Password reset request",
            source="email",
            sender="test@example.com",
            raw_text="Subject: Password reset\n\nBody: I need to reset my password",
        )
    )
    return client


@pytest.fixture
def mock_workflow(mock_llm_client):
    """Create a workflow with mocked LLM client."""
    from helpdesk_agent.core.workflow import HelpdeskWorkflow

    return HelpdeskWorkflow(mcp_adapter=None, llm_client=mock_llm_client)
