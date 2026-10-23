"""Pytest configuration and fixtures for helpdesk agent tests."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_LIVE_LLM_API_KEY = os.environ.get("HELPDESK_AGENT_LLM_API_KEY")

# Set a non-secret placeholder API key for tests
os.environ["HELPDESK_AGENT_LLM_API_KEY"] = "placeholder"
# Default to MCP disabled so webhook/chat tests get mocked flows unless a
# test explicitly enables it (e.g. test_mcp.py sets it before importing).
os.environ.setdefault("HELPDESK_AGENT_USE_MCP", "0")
os.environ["HELPDESK_AGENT_MEMORY_ENABLED"] = "0"
os.environ["HELPDESK_AGENT_LANGFUSE_ENABLED"] = "0"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-llm",
        action="store_true",
        default=False,
        help="Enable billable LLM integration tests against the configured base URL.",
    )
    parser.addoption(
        "--live-mcp",
        action="store_true",
        default=False,
        help="Enable external MCP discovery tests (initialize and tools/list only).",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    unit_modules = {
        "test_agentcore_deploy.py",
        "test_chat.py",
        "test_config.py",
        "test_faq.py",
        "test_memory.py",
        "test_ticketing.py",
    }
    for item in items:
        if Path(__file__).parent / "archive" in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.archived)
            continue
        if item.get_closest_marker("live_llm") and not item.config.getoption("--live-llm"):
            item.add_marker(pytest.mark.skip(reason="External LLM invocation requires --live-llm"))
        if item.get_closest_marker("live_mcp") and not item.config.getoption("--live-mcp"):
            item.add_marker(pytest.mark.skip(reason="External MCP discovery requires --live-mcp"))
        categories = [name for name in ("unit", "integration") if item.get_closest_marker(name)]
        if len(categories) > 1:
            raise pytest.UsageError(f"Test has multiple categories: {item.nodeid}")
        if categories:
            continue
        filename = Path(str(item.path)).name
        if filename in unit_modules:
            item.add_marker(pytest.mark.unit)
        else:
            raise pytest.UsageError(f"Assign a unit or integration marker to {item.nodeid}")


@pytest.fixture
def live_llm_config(monkeypatch: pytest.MonkeyPatch):
    from dotenv import dotenv_values

    from helpdesk_agent.config.settings import LLMConfig

    file_key = dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("HELPDESK_AGENT_LLM_API_KEY")
    key = _LIVE_LLM_API_KEY if _LIVE_LLM_API_KEY is not None else file_key
    if not key or not key.strip() or key == "placeholder":
        pytest.fail("Live LLM tests require a real HELPDESK_AGENT_LLM_API_KEY", pytrace=False)
    monkeypatch.setenv("HELPDESK_AGENT_LLM_API_KEY", key)
    return LLMConfig.from_env()


@pytest.fixture(autouse=True)
def _reset_dependency_caches():
    """Clear lru_cached dependencies so env/config changes are picked up."""
    import helpdesk_agent.api.dependencies as dependencies

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
    from helpdesk_agent.domain.models.browser import BrowserPlan, BrowserReactAction
    from helpdesk_agent.domain.models.triage import TriageReflection, TriageResult
    from helpdesk_agent.infrastructure.llm.client import LLMTriageClient

    client = MagicMock(spec=LLMTriageClient)
    result = TriageResult(
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
    client.triage_email = AsyncMock(return_value=result)
    client.reflect_triage = AsyncMock(
        return_value=TriageReflection(outcome="accept", assessment_summary="Supported by the report")
    )
    client.plan_browser = AsyncMock(
        return_value=BrowserPlan(
            objective="Inspect the reported page",
            inspection_order=["browser_snapshot"],
        )
    )
    client.react_browser = AsyncMock(
        return_value=BrowserReactAction(action="finish", decision_summary="The observations are sufficient")
    )
    client.draft_clarification = AsyncMock(return_value="We need a little more information.")
    client.draft_resolution = AsyncMock(return_value="Please try the supplied solution.")
    client.fallback_clarification = MagicMock(return_value="We need a little more information.")
    client.fallback_resolution = MagicMock(return_value="Please try the supplied solution.")
    return client


@pytest.fixture
def mock_workflow(mock_llm_client):
    """Create a workflow with mocked LLM client."""
    from helpdesk_agent.api.factory import HelpdeskWorkflow

    return HelpdeskWorkflow(mcp_adapter=None, llm_client=mock_llm_client)
