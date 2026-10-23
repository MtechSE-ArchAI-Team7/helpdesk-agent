from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.graph.state import CompiledStateGraph

from helpdesk_agent.api.factory import HelpdeskWorkflow
from helpdesk_agent.config import LLMConfig, MCPConfig
from helpdesk_agent.domain.models.browser import BrowserSettings, PlaywrightAssessment, PlaywrightEvaluation
from helpdesk_agent.domain.models.triage import TriageResult
from helpdesk_agent.infrastructure.llm.client import LLMTriageClient
from helpdesk_agent.workflows.state import GraphState

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args,response,fingerprint",
    [
        (
            "triage_email",
            ("Test subject", "Test body", ["USER: earlier report"]),
            {
                "service": "identity",
                "issue_type": "access",
                "priority": "medium",
                "confidence": 0.8,
                "missing_info": [],
                "clarifying_questions": [],
                "summary": "Test summary",
            },
            "b75c56928fa3b551719e3ed535568ba0c51782c8bdbb17bdeb47eba0e106fde3",
        ),
        (
            "assess_playwright_validation",
            ("Test subject", "Test body", "Test summary"),
            {
                "decision": "not_applicable",
                "reason": "Not browser visible",
                "target_url": None,
                "expected_error": "",
                "clarifying_questions": [],
                "missing_info": [],
            },
            "98eb23f6ca7617e0a8883aa8a1c59d0a99761ee23692e1cfa859f07e90abad37",
        ),
        (
            "evaluate_playwright_validation",
            ("Expected error", "Observed evidence"),
            {"status": "inconclusive", "summary": "Not enough evidence"},
            "faa2af60b1beda0f9722987967be33f2d8c87c8a8223277562a2de1dfa033d33",
        ),
    ],
)
async def test_prompt_messages_match_pre_refactor_fingerprints(method, args, response, fingerprint) -> None:
    client = LLMTriageClient(LLMConfig(api_key="placeholder"))
    client._complete_json = AsyncMock(return_value=response)
    try:
        await getattr(client, method)(*args)
        messages = client._complete_json.call_args.kwargs["messages"]
        assert hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest() == fingerprint
        client._complete_json.assert_awaited_once()
    finally:
        await client.client.close()


@pytest.mark.asyncio
async def test_email_fallback_templates_match_baseline_without_model_calls() -> None:
    client = LLMTriageClient(LLMConfig(api_key="placeholder"))
    client._complete_json = AsyncMock()
    triage = TriageResult(
        service="identity",
        issue_type="access",
        priority="medium",
        confidence=0.8,
        summary="Test summary",
        clarifying_questions=["Which account?"],
    )
    try:
        clarification = client.fallback_clarification(triage, "Test body")
        resolution = client.fallback_resolution({"title": "Test article", "body": "Test instructions"}, "Test body")
        assert hashlib.sha256(clarification.encode()).hexdigest() == (
            "86e6667eb431acf960513dc29319c2503cf1f59588f588fc13be6ec7d72b6724"
        )
        assert hashlib.sha256(resolution.encode()).hexdigest() == (
            "9d7c20db1e0d255db89b4a338ef581f8d1aff3d0844f8f453273065a9489a8b6"
        )
        client._complete_json.assert_not_awaited()
    finally:
        await client.client.close()


@pytest.mark.asyncio
async def test_compiled_browser_subgraph_is_used_on_eligible_parent_route() -> None:
    client = MagicMock(spec=LLMTriageClient)
    triage = TriageResult(
        service="identity",
        issue_type="access",
        priority="medium",
        confidence=0.9,
        summary="Login fails",
    )
    client.triage_email = AsyncMock(return_value=triage)
    from helpdesk_agent.domain.models.browser import BrowserPlan, BrowserReactAction

    client.reflect_triage = AsyncMock(side_effect=lambda result, _subject, _body: result)
    client.plan_browser = AsyncMock(
        return_value=BrowserPlan(objective="Inspect the login page", inspection_order=["browser_snapshot"])
    )
    client.react_browser = AsyncMock(
        return_value=BrowserReactAction(action="finish", decision_summary="Evidence is sufficient")
    )
    client.assess_playwright_validation = AsyncMock(
        return_value=PlaywrightAssessment(
            decision="eligible",
            reason="Visible failure",
            target_url="https://portal.example.com/login",
            expected_error="Login fails",
        )
    )
    client.evaluate_playwright_validation = AsyncMock(
        return_value=PlaywrightEvaluation(
            status="reproduced",
            summary="Login error displayed",
        )
    )
    workflow = HelpdeskWorkflow(llm_client=client)
    workflow.mcp_enabled = True
    workflow.browser_settings = BrowserSettings(True, ("portal.example.com",))
    workflow.playwright_flow.config = MCPConfig(playwright_allowed_hosts=["portal.example.com"])
    from contextlib import asynccontextmanager

    session = MagicMock()
    session.navigate = AsyncMock(return_value={"tool": "browser_navigate", "status": "completed", "content": {}})

    @asynccontextmanager
    async def open_session(_target_url):
        yield session

    workflow.playwright_flow.open_session = MagicMock(side_effect=open_session)
    assert isinstance(workflow.browser_graph, CompiledStateGraph)
    assert {"revalidate", "plan", "react", "evaluate", "record"} <= workflow.browser_graph.get_graph().nodes.keys()
    workflow.browser_graph.ainvoke = AsyncMock(wraps=workflow.browser_graph.ainvoke)
    state = await workflow.run(
        {
            "case_id": "browser-contract",
            "subject": "Login failure",
            "summary": "Login failure\n\nhttps://portal.example.com/login fails to log in",
            "source": "chat",
        }
    )
    workflow.browser_graph.ainvoke.assert_awaited_once()
    workflow.playwright_flow.open_session.assert_called_once_with("https://portal.example.com/login")
    assert state.current_step == "diagnosis_required"
    assert len(state.evidence) == 1
    assert state.evidence[0]["status"] == "reproduced"
    assert sum(entry["step"] == "playwright_validation" for entry in state.history) == 1
    browser_events = [event for event in state.events if event["stage"] == "playwright_validate"]
    assert [event["action"] for event in browser_events] == ["step.started", "step.finished"]
    assert not any(key.startswith("_browser") for key in state.to_dict())


@pytest.mark.asyncio
async def test_browser_subgraph_returns_parent_state_without_internal_channels() -> None:
    workflow = HelpdeskWorkflow(llm_client=MagicMock(spec=LLMTriageClient))
    from helpdesk_agent.domain.models.workflow import WorkflowState

    state: GraphState = {
        "workflow_state": WorkflowState(case_id="no-browser", summary="Question"),
        "initial_input": {},
        "triage_result": None,
        "faq_results": None,
        "ticket_id": None,
        "existing_ticket": None,
        "playwright_assessment": None,
        "playwright_validation": None,
    }
    before = set(state)
    result = await workflow._playwright_validate_node(state)
    assert result is state
    assert set(result) == before
    assert result["workflow_state"].history[-1]["status"] == "skipped"
