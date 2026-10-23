from unittest.mock import AsyncMock, MagicMock

import pytest

from helpdesk_agent.api.factory import HelpdeskWorkflow
from helpdesk_agent.config.settings import MCPConfig, TriageConfig
from helpdesk_agent.domain.models.browser import (
    BrowserPlan,
    BrowserReactAction,
    BrowserSettings,
    PlaywrightAssessment,
    PlaywrightEvaluation,
)
from helpdesk_agent.domain.models.triage import TriageResult
from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.infrastructure.a2a.client import DeliveryOutcome
from helpdesk_agent.infrastructure.llm.client import LLMError
from helpdesk_agent.infrastructure.memory.agentcore import MemoryError
from helpdesk_agent.observability.events import _record_gate_decision

pytestmark = pytest.mark.unit


def triage(**overrides):
    return TriageResult(
        **{
            "service": "identity",
            "issue_type": "access",
            "priority": "high",
            "confidence": 0.9,
            "missing_info": [],
            "clarifying_questions": [],
            "summary": "Login issue",
            "source": "email",
            "sender": "u@example.com",
            "raw_text": "Login issue",
            **overrides,
        }
    )


def graph_state(**overrides):
    return {
        "workflow_state": WorkflowState(case_id="HELP-1", summary="Login issue", ticket={"ticket_id": "HELP-1"}),
        "initial_input": {
            "subject": "Login issue",
            "summary": "Login issue\n\nhttps://portal.example.com/login",
            "sender": "u@example.com",
            "memory_identity": "message-1",
            "metadata": {"message_id": "123"},
        },
        "ticket_id": "HELP-1",
        "triage_result": triage(),
        "existing_ticket": None,
        "faq_results": None,
        "playwright_assessment": None,
        "playwright_validation": None,
        **overrides,
    }


@pytest.fixture
def workflow():
    llm = MagicMock()
    llm.triage_email = AsyncMock(return_value=triage())
    llm.reflect_triage = AsyncMock(side_effect=lambda result, _subject, _body: result)
    llm.plan_browser = AsyncMock(
        return_value=BrowserPlan(
            objective="Inspect the page", inspection_order=["browser_snapshot", "browser_console_messages"]
        )
    )
    llm.react_browser = AsyncMock(
        return_value=BrowserReactAction(action="finish", decision_summary="The observations are sufficient")
    )
    llm.draft_clarification = AsyncMock(return_value="Which browser?")
    llm.draft_resolution = AsyncMock(return_value="Reset your password.")
    llm.fallback_clarification = MagicMock(return_value="Which browser?")
    llm.fallback_resolution = MagicMock(return_value="Reset your password.")
    flow = HelpdeskWorkflow(llm_client=llm, triage_config=TriageConfig())
    flow.project_key = "HELP"
    flow.ticket_flow = MagicMock()
    flow.ticket_flow.get_ticket = AsyncMock(return_value={"status": "success", "ticket_id": "HELP-1", "comments": []})
    flow.ticket_flow.create_ticket = AsyncMock(return_value={"status": "created", "ticket_id": "HELP-1"})
    flow.ticket_flow.add_comment = AsyncMock(return_value={"status": "commented"})
    flow.ticket_flow.update_ticket = AsyncMock(return_value={"status": "updated", "updated": ["summary"]})
    flow.mail_flow.reply_email = AsyncMock(return_value={"status": "sent", "reply_message_id": "456"})
    flow.mail_flow.get_messages = AsyncMock(
        return_value={"status": "success", "messages": [{"subject": "Help"}], "count": 1}
    )
    flow.faq_flow.search = AsyncMock(return_value={"status": "success", "results": [], "count": 0})
    return flow


def browser_enabled(flow):
    flow.mcp_enabled = True
    flow.browser_settings = BrowserSettings(True, ("portal.example.com",))
    flow.mcp_adapter = MagicMock()
    flow.playwright_flow.config = MCPConfig(
        playwright_url="https://browser.example.com/mcp", playwright_allowed_hosts=["portal.example.com"]
    )
    flow.playwright_flow.mcp_adapter = flow.mcp_adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup,linked", [("success", False), ("failed", False), ("failed", True)])
async def test_ticket_lookup_success_stale_reference_and_link_failure(workflow, lookup, linked):
    state = graph_state()
    state["initial_input"]["subject"] = "Re: [HELP-1] Login"
    workflow.ticket_flow.get_ticket.return_value = {"status": lookup, "ticket_id": "HELP-1"}
    if linked:
        workflow.memory = MagicMock()
        state["initial_input"]["ticket_id"] = "HELP-1"
        with pytest.raises(MemoryError, match="refusing to create another"):
            await workflow._load_ticket_node(state)
        assert state["workflow_state"].events[-1]["status"] == "FAILED"
    else:
        await workflow._load_ticket_node(state)
        assert workflow._has_existing_ticket(state) == ("existing" if lookup == "success" else "new")
        assert state["ticket_id"] == ("HELP-1" if lookup == "success" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"status": "failed", "error": "Unavailable"}, {"status": "created"}])
async def test_ticket_creation_failure_stops_graph(workflow, result):
    workflow.ticket_flow.create_ticket.return_value = result
    final = await workflow.run({"case_id": "new", "summary": "Login"})
    assert final.current_step == "ticket_creation_failed"
    workflow.mail_flow.reply_email.assert_not_awaited()
    workflow.llm_client.triage_email.assert_not_awaited()
    assert final.events[-1]["details"]["terminal_outcome"] == "ticket_creation_failed"


@pytest.mark.parametrize(
    "rounds,eligible,confidence,expected",
    [
        (0, False, 0.9, "faq_gate"),
        (0, True, 0.9, "playwright"),
        (0, False, 0.3, "clarify"),
        (2, False, 0.3, "diagnose"),
        (2, True, 0.3, "playwright"),
    ],
)
def test_clarification_gate_threshold_and_round_limit(workflow, rounds, eligible, confidence, expected):
    state = graph_state(
        triage_result=triage(confidence=confidence),
        existing_ticket={"comments": [{"body": "Pending clarifications"}] * rounds + ["Other note"]},
    )
    if eligible:
        state["playwright_assessment"] = PlaywrightAssessment(
            decision="eligible", reason="Allowed", target_url="https://portal.example.com/login"
        )
    assert workflow._should_clarify(state) == expected
    assert state["workflow_state"].events[-1]["details"]["decision"] == expected


def test_gate_without_triage_and_invalid_state(workflow):
    state = graph_state(triage_result=None)
    assert workflow._should_clarify(state) == "faq_gate"
    with pytest.raises(TypeError, match="WorkflowState"):
        _record_gate_decision({"workflow_state": None}, stage="gate", decision="stop", reason_code="invalid")
    assert workflow._ticket_created({"ticket_id": None}) == "failed"
    assert workflow._ticketed_subject(None, "Help") == "Help"
    assert workflow._ticketed_subject("HELP-1", "Re: [HELP-1] Help") == "Re: [HELP-1] Help"
    assert workflow._split_subject_body({"summary": "Help"}) == ("Help", "Help")
    assert workflow._text_to_html("- A & B\n\n<Hello>") == "<ul><li>A &amp; B</li></ul>\n<p>&lt;Hello&gt;</p>"
    assert "playwright_validate" in workflow.create_langgraph_plan()


@pytest.mark.asyncio
@pytest.mark.parametrize("node", ["_clarify_node", "_faq_gate_node", "_update_ticket_node", "_resolve_faq_node"])
async def test_nodes_handle_missing_preconditions(workflow, node):
    state = graph_state(triage_result=None)
    await getattr(workflow, node)(state)
    workflow.llm_client.draft_clarification.assert_not_awaited()
    workflow.llm_client.draft_resolution.assert_not_awaited()
    workflow.ticket_flow.update_ticket.assert_not_awaited()
    workflow.faq_flow.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("faq_score", [0.69, 0.7, 0.95])
@pytest.mark.parametrize("email", [False, True])
async def test_faq_resolution_and_diagnosis_graph_with_mocked_flows(workflow, faq_score, email):
    workflow.faq_flow.search.return_value = {
        "status": "success",
        "count": 1,
        "results": [{"title": "Password reset", "body": "Reset steps", "score": faq_score}],
    }
    initial = {"case_id": "new", "summary": "Login", "sender": "u@example.com"}
    if email:
        initial["metadata"] = {"message_id": "123"}
    final = await workflow.run(initial)
    assert final.current_step == ("faq_answer_provided" if faq_score >= 0.7 else "diagnosis_required")
    if faq_score >= 0.7:
        if email:
            workflow.llm_client.draft_resolution.assert_awaited_once()
        else:
            workflow.llm_client.draft_resolution.assert_not_awaited()
        assert "FAQ answer provided" in workflow.ticket_flow.add_comment.await_args.args[1]
    else:
        workflow.llm_client.draft_resolution.assert_not_awaited()
        assert "Diagnosis required" in workflow.ticket_flow.add_comment.await_args.args[1]
    assert workflow.llm_client.draft_resolution.await_count == int(email and faq_score >= 0.7)
    assert workflow.mail_flow.reply_email.await_count == (2 if email and faq_score >= 0.7 else int(email))


@pytest.mark.asyncio
@pytest.mark.parametrize("sent,ticket", [(True, True), (False, True), (True, False)])
async def test_clarification_delivery_and_comments(workflow, sent, ticket):
    workflow.mail_flow.reply_email.return_value = {"status": "sent" if sent else "failed"}
    state = graph_state(ticket_id="HELP-1" if ticket else None)
    await workflow._clarify_node(state)
    assert state["workflow_state"].current_step == "awaiting_clarification"
    assert bool(state["workflow_state"].messages) is sent
    assert workflow.ticket_flow.add_comment.await_count == int(ticket)
    if ticket:
        assert "general details requested" in workflow.ticket_flow.add_comment.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", ["runtime", "allowlist"])
async def test_browser_assessment_requires_runtime_and_allowlist(workflow, skip):
    workflow.mcp_adapter = MagicMock()
    workflow.mcp_enabled = True
    workflow.playwright_flow.config = MCPConfig(
        playwright_url="https://browser.example.com" if skip == "allowlist" else None,
    )
    state = graph_state()
    await workflow._assess_playwright_node(state)
    assert state["playwright_assessment"] is None
    assert state["workflow_state"].history[-1]["status"] == "skipped"
    workflow.llm_client.assess_playwright_validation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["error", "untrusted", "missing_error", "needs_info", "eligible"])
async def test_browser_feasibility_enforces_targets_and_merges_questions(workflow, case):
    browser_enabled(workflow)
    state = graph_state()
    assessment = PlaywrightAssessment(
        decision="needs_info" if case == "needs_info" else "eligible",
        reason="Check page",
        target_url="https://untrusted.example/login" if case == "untrusted" else "https://portal.example.com/login",
        expected_error="" if case == "missing_error" else "Login fails",
    )
    workflow.llm_client.assess_playwright_validation = AsyncMock(
        side_effect=LLMError("Unavailable") if case == "error" else None,
        return_value=assessment,
    )
    await workflow._assess_playwright_node(state)
    result = state["playwright_assessment"]
    if case == "error":
        assert result is None
        assert state["workflow_state"].history[-1]["status"] == "failed"
    elif case == "untrusted":
        assert result.decision == "not_applicable"
        assert result.target_url is None
    elif case in {"missing_error", "needs_info"}:
        assert result.decision == "needs_info"
        assert state["triage_result"].missing_info
        assert state["triage_result"].clarifying_questions
        assert workflow._should_clarify(state) == "clarify"
    else:
        assert result.decision == "eligible"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "untrusted", "tool_failed", "evaluation_failed", "completed"])
async def test_browser_validation_evidence_failures_and_ticket_comment(workflow, case):
    browser_enabled(workflow)
    state = graph_state()
    if case != "missing":
        state["playwright_assessment"] = PlaywrightAssessment(
            decision="eligible",
            reason="Check page",
            expected_error="Login fails",
            target_url="https://untrusted.example/login" if case == "untrusted" else "https://portal.example.com/login",
        )
    from contextlib import asynccontextmanager

    session = MagicMock()
    session.navigate = AsyncMock(return_value={"tool": "browser_navigate", "status": "completed", "content": {}})
    session.inspect = AsyncMock(
        return_value={
            "tool": "browser_snapshot",
            "status": "failed" if case == "tool_failed" else "completed",
            "content": {"text": "Login error"},
        }
    )

    @asynccontextmanager
    async def open_session(_target_url):
        yield session

    workflow.playwright_flow.open_session = MagicMock(side_effect=open_session)
    workflow.llm_client.plan_browser.return_value = BrowserPlan(
        objective="Inspect the login page", inspection_order=["browser_snapshot"]
    )
    if case == "tool_failed":
        workflow.llm_client.react_browser.return_value = BrowserReactAction(
            action="inspect", tool="browser_snapshot", decision_summary="Inspect page content"
        )
    workflow.llm_client.evaluate_playwright_validation = AsyncMock(
        side_effect=LLMError("Unavailable") if case == "evaluation_failed" else None,
        return_value=PlaywrightEvaluation(status="reproduced", summary="Login error displayed"),
    )
    await workflow._playwright_validate_node(state)
    if case in {"missing", "untrusted"}:
        workflow.playwright_flow.open_session.assert_not_called()
        assert state["workflow_state"].history[-1]["status"] == "skipped"
    else:
        workflow.playwright_flow.open_session.assert_called_once_with("https://portal.example.com/login")
        expected = "reproduced" if case == "completed" else "inconclusive"
        assert state["playwright_validation"]["status"] == expected
        assert state["workflow_state"].evidence[0]["status"] == expected
        if case == "tool_failed":
            workflow.ticket_flow.add_comment.assert_not_awaited()
        else:
            assert expected in workflow.ticket_flow.add_comment.await_args.args[1]


@pytest.mark.asyncio
async def test_triage_reflection_failure_retains_initial_result(workflow):
    state = graph_state()
    workflow.llm_client.reflect_triage.side_effect = LLMError("Reflection unavailable")
    await workflow._triage_node(state)
    assert state["triage_result"].summary == "Login issue"
    assert state["workflow_state"].triage["summary"] == "Login issue"
    assert any(
        entry["step"] == "triage_reflection" and entry["status"] == "failed"
        for entry in state["workflow_state"].history
    )


@pytest.mark.asyncio
async def test_email_generation_failure_uses_deterministic_fallback(workflow):
    state = graph_state()
    workflow.llm_client.draft_clarification.side_effect = LLMError("Email model unavailable")
    workflow.llm_client.fallback_clarification.return_value = "Fallback clarification"
    await workflow._clarify_node(state)
    assert workflow.mail_flow.reply_email.await_args.args[1] == "Fallback clarification"
    workflow.llm_client.fallback_clarification.assert_called_once()
    assert any(
        entry["step"] == "email_generation" and entry["status"] == "failed" for entry in state["workflow_state"].history
    )


@pytest.mark.asyncio
async def test_browser_planning_failure_skips_tool_session(workflow):
    browser_enabled(workflow)
    state = graph_state(
        playwright_assessment=PlaywrightAssessment(
            decision="eligible",
            reason="Check the reported error",
            expected_error="Login fails",
            target_url="https://portal.example.com/login",
        )
    )
    workflow.llm_client.plan_browser.side_effect = LLMError("Planning unavailable")
    workflow.playwright_flow.open_session = MagicMock()
    await workflow._playwright_validate_node(state)
    assert state["playwright_validation"]["status"] == "inconclusive"
    workflow.playwright_flow.open_session.assert_not_called()
    workflow.ticket_flow.add_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_react_stops_after_three_inspection_rounds(workflow):
    from contextlib import asynccontextmanager

    browser_enabled(workflow)
    state = graph_state(
        playwright_assessment=PlaywrightAssessment(
            decision="eligible",
            reason="Check the reported error",
            expected_error="Login fails",
            target_url="https://portal.example.com/login",
        )
    )
    workflow.llm_client.plan_browser.return_value = BrowserPlan(
        objective="Inspect relevant page evidence",
        inspection_order=["browser_snapshot", "browser_console_messages", "browser_network_requests"],
    )
    workflow.llm_client.react_browser.side_effect = [
        BrowserReactAction(action="inspect", tool=tool, decision_summary="Collect the next observation")
        for tool in ["browser_snapshot", "browser_console_messages", "browser_network_requests"]
    ]
    session = MagicMock()
    session.navigate = AsyncMock(return_value={"tool": "browser_navigate", "status": "completed", "content": {}})
    session.inspect = AsyncMock(
        side_effect=lambda tool: {"tool": tool, "status": "completed", "content": {"text": "observed"}}
    )

    @asynccontextmanager
    async def open_session(_target_url):
        yield session

    workflow.playwright_flow.open_session = MagicMock(side_effect=open_session)
    workflow.llm_client.evaluate_playwright_validation = AsyncMock(
        return_value=PlaywrightEvaluation(status="reproduced", summary="Error found")
    )
    await workflow._playwright_validate_node(state)
    assert workflow.llm_client.react_browser.await_count == 3
    assert session.inspect.await_count == 3
    assert state["playwright_validation"]["status"] == "reproduced"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [DeliveryOutcome(True, task_id="task-1"), DeliveryOutcome(False, reason="Offline"), RuntimeError("Offline")],
)
async def test_diagnosis_handoff_records_delivery_and_manual_fallback(workflow, outcome):
    workflow.a2a_client_manager = MagicMock()
    workflow.a2a_client_manager.has_target.return_value = True
    workflow.a2a_client_manager.send_case = AsyncMock(
        side_effect=outcome if isinstance(outcome, Exception) else None,
        return_value=outcome,
    )
    state = graph_state(existing_ticket={"comments": ["Pending clarifications", {"body": "pending clarifications"}]})
    await workflow._diagnose_node(state)
    trigger = state["workflow_state"].metadata["diagnosis_trigger"]
    assert trigger["submitted"] is (isinstance(outcome, DeliveryOutcome) and outcome.submitted)
    assert state["workflow_state"].current_step == "diagnosis_required"
    assert "attempts exhausted" in workflow.ticket_flow.add_comment.await_args_list[0].args[1]
    if not trigger["submitted"]:
        assert "Manual investigation required" in workflow.ticket_flow.add_comment.await_args.args[1]


@pytest.mark.asyncio
async def test_diagnosis_without_ticket_or_triage(workflow):
    state = graph_state(ticket_id=None, triage_result=None)
    await workflow._diagnose_node(state)
    assert state["workflow_state"].current_step == "diagnosis_required"
    workflow.ticket_flow.add_comment.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["chat", "a2a"])
@pytest.mark.parametrize("failed", [False, True])
async def test_mailbox_read_bypasses_triage_and_records_failures(workflow, source, failed):
    if failed:
        workflow.mail_flow.get_messages.side_effect = RuntimeError("Mailbox unavailable")
        with pytest.raises(RuntimeError, match="Mailbox"):
            await workflow.run({"summary": "Read email", "source": source})
    else:
        result = await workflow.run({"summary": "Read email", "source": source})
        assert result.current_step == "email_retrieved"
        assert result.metadata["email_messages"] == [{"subject": "Help"}]
        assert result.events[-1]["status"] == "COMPLETED"
    workflow.llm_client.triage_email.assert_not_awaited()
    workflow.ticket_flow.create_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_triage_failure_propagates_and_records_failed_events(workflow):
    state = graph_state()
    workflow.llm_client.triage_email.side_effect = LLMError("Model unavailable")
    with pytest.raises(LLMError):
        await workflow._triage_node(state)
    assert state["workflow_state"].events[-1]["status"] == "FAILED"
    with pytest.raises(LLMError):
        await workflow.run({"summary": "Login"})


@pytest.mark.asyncio
async def test_memory_history_failure_does_not_skip_other_memory_operations(workflow):
    workflow.memory = MagicMock()
    workflow.memory.context_chars = 1000
    workflow.memory.history = AsyncMock(side_effect=MemoryError("History unavailable"))
    workflow.memory.similar = AsyncMock(side_effect=MemoryError("Search unavailable"))
    workflow.memory.save_message = AsyncMock(side_effect=MemoryError("Write unavailable"))
    workflow.memory.save_snapshot = AsyncMock(side_effect=MemoryError("Snapshot unavailable"))
    workflow.memory.bind_conversation = AsyncMock()
    state = graph_state(existing_ticket={"ticket_id": "HELP-1", "comments": ["Existing report"]})
    state["initial_input"]["metadata"]["conversation_id"] = "conversation-1"
    await workflow._triage_node(state)
    await workflow.save_chat_response(state["workflow_state"], "Reply", "response-1")
    await workflow._save_ticket_memory(state)
    assert {error["operation"] for error in state["workflow_state"].metadata["memory_errors"]} == {
        "memory_history",
        "memory_similar",
        "memory_save_user",
        "memory_save_response",
        "memory_snapshot",
    }
    workflow.memory.bind_conversation.assert_awaited_once_with("conversation-1", "HELP-1")
    workflow.llm_client.triage_email.assert_awaited_once()
    assert state["triage_result"].service == "identity"


@pytest.mark.asyncio
async def test_browser_context_uses_only_same_ticket_user_reports(workflow):
    workflow.memory = MagicMock()
    state = graph_state()
    state["workflow_state"].metadata["ticket_memory"] = [
        "USER: https://portal.example.com",
        "ASSISTANT: https://other.example.com",
    ]
    _, body = workflow._browser_report(state)
    assert "USER: https://portal.example.com" in body
    assert "ASSISTANT:" not in body
    workflow.memory = None
    await workflow._save_incoming_memory(state, "HELP-1")
    await workflow._save_ticket_memory(state)
