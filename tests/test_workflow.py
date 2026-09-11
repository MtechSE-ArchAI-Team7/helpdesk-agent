from unittest.mock import AsyncMock, MagicMock

import pytest

from helpdesk_agent.core.workflow import HelpdeskWorkflow
from helpdesk_agent.modules.ticketing.service import extract_ticket_id
from helpdesk_agent.modules.triage.schema import TriageResult


def _triage_result(confidence=0.9, missing_info=None, questions=None):
    return TriageResult(
        service="identity",
        issue_type="access",
        priority="high",
        confidence=confidence,
        missing_info=missing_info or [],
        clarifying_questions=questions or [],
        summary="Password reset request",
        source="email",
        sender="test@example.com",
        raw_text="Subject: Password reset\n\nBody: I need to reset my password",
    )


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client."""
    mock = MagicMock()
    mock.triage_email = AsyncMock(return_value=_triage_result())
    mock.draft_clarification = AsyncMock(return_value="Please answer these questions.")
    mock.draft_resolution = AsyncMock(return_value="Here is the solution.")
    return mock


def test_extract_ticket_id_matches_project_key() -> None:
    assert extract_ticket_id("Re: [HELP-123] Login issues", "HELP") == "HELP-123"
    assert extract_ticket_id("HELP-1 something", "HELP") == "HELP-1"
    assert extract_ticket_id("Login issues", "HELP") is None
    # Other project keys must not match
    assert extract_ticket_id("Re: [OPS-55] question", "HELP") is None
    assert extract_ticket_id("Re: [OPS-55] question", "OPS") == "OPS-55"


@pytest.mark.asyncio
async def test_new_issue_creates_ticket_and_diagnoses(mock_llm_client) -> None:
    """No ticket ID in subject -> new ticket, triage, update, FAQ mocked -> diagnose."""
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    state = await workflow.run({"case_id": "case-001", "summary": "Password reset", "service": "identity"})

    # Ticket created in mocked mode; case_id now carries the ticket ID
    assert state.ticket is not None
    assert state.ticket["ticket_id"] == "JIRA-case-001"
    assert state.case_id == "JIRA-case-001"
    # FAQ search returns mocked (no results) -> diagnosis required comment path
    assert state.current_step == "diagnosis_required"
    steps = [entry["step"] for entry in state.history]
    assert "create_ticket" in steps
    assert "triage" in steps
    assert "update_ticket" in steps
    assert "diagnose" in steps


@pytest.mark.asyncio
async def test_new_issue_sends_acknowledgment_with_ticket_subject(mock_llm_client) -> None:
    """New issue with a message_id -> Zoho acknowledgment reply with [TICKET-ID] subject."""
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    workflow.mail_flow.reply_email = AsyncMock(return_value={"status": "sent", "reply_message_id": "r-1"})

    state = await workflow.run(
        {
            "case_id": "case-002",
            "summary": "Login issues\n\nI cannot log in",
            "subject": "Login issues",
            "source": "email",
            "sender": "alice@example.com",
            "metadata": {"message_id": "<msg-1@example.com>"},
        }
    )

    workflow.mail_flow.reply_email.assert_awaited()
    args, kwargs = workflow.mail_flow.reply_email.await_args
    assert args[0] == "<msg-1@example.com>"
    assert kwargs["subject"] == "[JIRA-case-002] Login issues"
    assert "JIRA-case-002" in args[1]


@pytest.mark.asyncio
async def test_clarification_sends_reply_and_comments_ticket(mock_llm_client) -> None:
    """Low confidence -> clarify: Zoho reply with questions + Jira comment."""
    mock_llm_client.triage_email = AsyncMock(
        return_value=_triage_result(confidence=0.3, missing_info=["error code"], questions=["What error code?"])
    )
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    workflow.mail_flow.reply_email = AsyncMock(return_value={"status": "sent", "reply_message_id": "r-2"})
    workflow.ticket_flow.add_comment = AsyncMock(return_value={"status": "commented", "ticket_id": "JIRA-case-003"})

    state = await workflow.run(
        {
            "case_id": "case-003",
            "summary": "Something broke\n\nIt broke",
            "subject": "Something broke",
            "source": "email",
            "sender": "bob@example.com",
            "metadata": {"message_id": "<msg-2@example.com>"},
        }
    )

    assert state.current_step == "awaiting_clarification"
    # Clarifying questions emailed to the user
    subjects = [c.kwargs.get("subject") for c in workflow.mail_flow.reply_email.await_args_list]
    assert "[JIRA-case-003] Something broke" in subjects
    # Ticket commented as pending clarifications
    comment_bodies = [c.args[1] for c in workflow.ticket_flow.add_comment.await_args_list]
    assert any("pending clarifications" in body.lower() for body in comment_bodies)


@pytest.mark.asyncio
async def test_existing_ticket_uses_ticket_context_and_updates_fields(mock_llm_client) -> None:
    """Ticket ID in subject -> load ticket, triage with ticket context, update fields."""
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    workflow.ticket_flow.get_ticket = AsyncMock(
        return_value={
            "status": "success",
            "ticket_id": "HELP-123",
            "summary": "Login issues",
            "description": "User cannot log in",
            "priority": "medium",
            "comments": ["Previous comment"],
        }
    )
    workflow.ticket_flow.update_ticket = AsyncMock(return_value={"status": "updated", "ticket_id": "HELP-123"})

    state = await workflow.run(
        {
            "case_id": "email-x",
            "summary": "Re: [HELP-123] Login issues\n\nHere is the error code: E42",
            "subject": "Re: [HELP-123] Login issues",
            "source": "email",
            "sender": "carol@example.com",
        }
    )

    # Existing ticket loaded; no new ticket created
    assert state.case_id == "HELP-123"
    assert state.ticket["ticket_id"] == "HELP-123"
    # Triage received the ticket context
    _, kwargs = mock_llm_client.triage_email.await_args
    snippets = kwargs.get("history_snippets") or []
    assert any("HELP-123" in s for s in snippets)
    # Ticket fields updated after triage
    workflow.ticket_flow.update_ticket.assert_awaited_once()
    _, update_kwargs = workflow.ticket_flow.update_ticket.await_args
    assert update_kwargs["priority"] == "high"


@pytest.mark.asyncio
async def test_stale_ticket_id_falls_back_to_new_issue(mock_llm_client) -> None:
    """Ticket ID in subject but not found in Jira -> treated as a new issue."""
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    workflow.ticket_flow.get_ticket = AsyncMock(
        return_value={"status": "failed", "ticket_id": "HELP-999", "error": "not found"}
    )

    state = await workflow.run(
        {
            "case_id": "email-y",
            "summary": "Re: [HELP-999] Old issue\n\nStill broken",
            "subject": "Re: [HELP-999] Old issue",
            "source": "email",
            "sender": "dave@example.com",
        }
    )

    # Fell through to new-ticket path
    assert any(entry["step"] == "create_ticket" for entry in state.history)
    assert any(
        entry["step"] == "load_ticket" and entry["status"] == "not_found" for entry in state.history
    )
    assert state.ticket["ticket_id"] == "JIRA-email-y"


@pytest.mark.asyncio
async def test_clarification_round_cap_escalates_to_diagnosis(mock_llm_client) -> None:
    """After 2 'pending clarifications' comments, clarification escalates to diagnose."""
    mock_llm_client.triage_email = AsyncMock(
        return_value=_triage_result(confidence=0.2, missing_info=["still unclear"], questions=["More details?"])
    )
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    workflow.ticket_flow.get_ticket = AsyncMock(
        return_value={
            "status": "success",
            "ticket_id": "HELP-55",
            "summary": "Vague issue",
            "description": "Unclear",
            "priority": "low",
            "comments": [
                {"body": "Pending clarifications — questions sent to the requester:\n- Q1"},
                {"body": "Pending clarifications — questions sent to the requester:\n- Q2"},
            ],
        }
    )
    workflow.mail_flow.reply_email = AsyncMock(return_value={"status": "sent"})
    workflow.ticket_flow.add_comment = AsyncMock(return_value={"status": "commented"})

    state = await workflow.run(
        {
            "case_id": "email-z",
            "summary": "Re: [HELP-55] Vague issue\n\nMore of the same",
            "subject": "Re: [HELP-55] Vague issue",
            "source": "email",
            "sender": "erin@example.com",
            "metadata": {"message_id": "<msg-3@example.com>"},
        }
    )

    assert state.current_step == "diagnosis_required"
    # No clarification email this time (ack/resolve not applicable either)
    workflow.mail_flow.reply_email.assert_not_awaited()
    comment_bodies = [c.args[1] for c in workflow.ticket_flow.add_comment.await_args_list]
    assert any("clarification attempts exhausted" in body.lower() for body in comment_bodies)


@pytest.mark.asyncio
async def test_ticket_creation_failure_stops_run(mock_llm_client) -> None:
    """If Jira ticket creation fails, stop before ack/triage — no phantom ID."""
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    workflow.ticket_flow.create_ticket = AsyncMock(
        return_value={"status": "failed", "error": "connection refused"}
    )
    workflow.mail_flow.reply_email = AsyncMock(return_value={"status": "sent"})

    state = await workflow.run(
        {
            "case_id": "case-fail",
            "summary": "VPN is down",
            "source": "email",
            "sender": "ops@example.com",
            "metadata": {"message_id": "<msg-x@example.com>"},
        }
    )

    assert state.current_step == "ticket_creation_failed"
    # No acknowledgment email, no triage
    workflow.mail_flow.reply_email.assert_not_awaited()
    mock_llm_client.triage_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_read_bypass(mock_llm_client) -> None:
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    state = await workflow.run({"case_id": "case-010", "summary": "read my emails"})
    assert state.current_step == "email_retrieved"
