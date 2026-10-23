import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import ANY, Stubber

from helpdesk_agent.api.factory import HelpdeskWorkflow
from helpdesk_agent.application.chat import handle_chat_message
from helpdesk_agent.application.diagnosis import build_case_input
from helpdesk_agent.config.settings import MemoryConfig
from helpdesk_agent.domain.models.triage import TriageResult
from helpdesk_agent.infrastructure.memory.agentcore import AgentCoreMemory, MemoryError, redact_shared_text
from helpdesk_agent.infrastructure.persistence.conversations import ConversationStore

MEMORY_ID = "HelpdeskMemory-abcdefghij"


def config(**overrides):
    return MemoryConfig(enabled=True, memory_id=MEMORY_ID, region="us-west-2", organization_id="acme", **overrides)


def aws_client():
    return boto3.client(
        "bedrock-agentcore",
        region_name="us-west-2",
        config=Config(signature_version=UNSIGNED),
    )


def event(text, role="USER", identity="earlier:USER"):
    return {
        "memoryId": MEMORY_ID,
        "actorId": "acme",
        "sessionId": "HELP-1",
        "eventId": "123#abc",
        "eventTimestamp": datetime.now(timezone.utc),
        "metadata": {"request_id": {"stringValue": hashlib.sha256(identity.encode()).hexdigest()}},
        "payload": [{"conversational": {"role": role, "content": {"text": text}}}],
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_id": ""},
        {"region": ""},
        {"organization_id": "../other"},
        {"history_events": 101},
        {"similar_cases": 0},
        {"context_chars": 50},
        {"timeout_seconds": 0},
    ],
)
def test_invalid_config_rejected(overrides):
    arguments = {"enabled": True, "memory_id": MEMORY_ID, "region": "us-west-2", "organization_id": "acme"}
    with pytest.raises(ValueError):
        MemoryConfig(**{**arguments, **overrides})


def test_disabled_memory_does_not_contact_aws(monkeypatch):
    from helpdesk_agent.api.dependencies import get_memory

    monkeypatch.setenv("HELPDESK_AGENT_MEMORY_ENABLED", "0")
    with patch("boto3.client") as client:
        assert get_memory() is None
        client.assert_not_called()


@pytest.mark.parametrize("status,has_strategies", [("CREATING", True), ("ACTIVE", False), ("ACTIVE", True)])
def test_startup_validates_resource_and_strategy_namespaces(status, has_strategies):
    from helpdesk_agent.infrastructure.memory.agentcore import FACTS_NAMESPACE, SUMMARY_NAMESPACE

    control = MagicMock()
    control.get_memory.return_value = {
        "memory": {
            "status": status,
            "strategies": [
                {"status": "ACTIVE", "namespaceTemplates": [SUMMARY_NAMESPACE]},
                {"status": "ACTIVE", "namespaceTemplates": [FACTS_NAMESPACE]},
            ]
            if has_strategies
            else [],
        }
    }
    data = MagicMock()
    with patch("boto3.client", side_effect=[control, data]):
        if status == "ACTIVE" and has_strategies:
            assert AgentCoreMemory(config()).client is data
        else:
            with pytest.raises(MemoryError, match="must be ACTIVE"):
                AgentCoreMemory(config())


@pytest.mark.asyncio
async def test_raw_turn_skips_extraction_and_snapshot_triggers_it():
    client = aws_client()
    memory = AgentCoreMemory(config(), client)
    scope = {"memoryId": MEMORY_ID, "actorId": "acme", "sessionId": "HELP-1"}
    with Stubber(client) as stub:
        stub.add_response(
            "create_event",
            {"event": event("Original report")},
            {
                **scope,
                "eventTimestamp": ANY,
                "clientToken": hashlib.sha256(b"acme:HELP-1:message-1:USER").hexdigest(),
                "metadata": ANY,
                "payload": [{"conversational": {"role": "USER", "content": {"text": "Original report"}}}],
                "extractionMode": "SKIP",
            },
        )
        stub.add_response(
            "create_event",
            {"event": event("Shared snapshot")},
            {**scope, "eventTimestamp": ANY, "clientToken": ANY, "metadata": ANY, "payload": ANY},
        )
        await memory.save_message("HELP-1", "USER", "Original report", "message-1")
        await memory.save_snapshot("HELP-1", {"report": "Email alice@example.com password=hidden"}, "message-1")
        stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_history_paginates_orders_and_excludes_replayed_incoming_message():
    client = aws_client()
    memory = AgentCoreMemory(config(history_events=3), client)
    arguments = {
        "memoryId": MEMORY_ID,
        "actorId": "acme",
        "sessionId": "HELP-1",
        "includePayloads": True,
        "maxResults": 3,
    }
    with Stubber(client) as stub:
        stub.add_response(
            "list_events",
            {
                "events": [event("Current report", identity="message-1:USER"), event("Which browser?", "ASSISTANT")],
                "nextToken": "next",
            },
            arguments,
        )
        stub.add_response(
            "list_events", {"events": [event("Login fails")]}, {**arguments, "nextToken": "next", "maxResults": 1}
        )
        assert await memory.history("HELP-1", "message-1") == ["USER: Login fails", "ASSISTANT: Which browser?"]
        stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_history_context_has_a_measured_size_bound():
    backend = MemoryBackend()
    memory = AgentCoreMemory(config(context_chars=1000), backend)
    await memory.save_message("HELP-1", "USER", "old report" * 500, "old")
    await memory.save_message("HELP-1", "ASSISTANT", "latest questions" * 500, "latest")
    snippets = await memory.history("HELP-1", "new")
    assert sum(len(snippet) for snippet in snippets) == 1000
    assert snippets[0].startswith("ASSISTANT: latest questions")


@pytest.mark.asyncio
async def test_similar_cases_scoped_excludes_current_ticket_and_combines_findings():
    client = aws_client()
    memory = AgentCoreMemory(config(similar_cases=2), client)

    def record(ticket, text, prefix="/org/acme/tickets/"):
        return {
            "memoryRecordId": "mem-" + "a" * 36,
            "memoryStrategyId": "summary-1",
            "createdAt": datetime.now(timezone.utc),
            "namespaces": [f"{prefix}{ticket}/summary/"],
            "content": {"text": text},
            "score": 0.8,
        }

    with Stubber(client) as stub:
        stub.add_response(
            "retrieve_memory_records",
            {
                "memoryRecordSummaries": [
                    record("HELP-1", "Current case"),
                    record("HELP-2", "Login fails"),
                    record("HELP-2", "Clearing cache did not help"),
                    record("HELP-3", "Another organization", "/org/other/tickets/"),
                    record("HELP-4", "Another affected user"),
                ]
            },
            {
                "memoryId": MEMORY_ID,
                "namespacePath": "/org/acme/tickets/",
                "searchCriteria": {"searchQuery": "Login fails for [EMAIL]", "topK": 8},
            },
        )
        cases = await memory.similar("HELP-1", "Login fails for alice@example.com")
        assert [case["ticket_id"] for case in cases] == ["HELP-2", "HELP-4"]
        assert "Clearing cache did not help" in cases[0]["content"]
        assert "not a verified fix" in cases[0]["qualification"]


@pytest.mark.asyncio
async def test_aws_errors_are_explicit_without_logging_sensitive_request(caplog):
    client = aws_client()
    memory = AgentCoreMemory(config(), client)
    with Stubber(client) as stub:
        stub.add_client_error(
            "create_event", service_error_code="AccessDeniedException", service_message="private content"
        )
        with pytest.raises(MemoryError, match="create_event failed"):
            await memory.save_message("HELP-1", "USER", "private content", "message-1")
    assert "private content" not in caplog.text


def test_shared_redaction():
    text = redact_shared_text('alice@example.com password="hidden value" api_key=hidden Authorization: Bearer hidden')
    assert "alice@example.com" not in text
    assert "hidden" not in text
    assert "[REDACTED]" in text


def test_browser_context_only_uses_this_tickets_user_reports():
    from helpdesk_agent.domain.models.workflow import WorkflowState

    run = workflow(AgentCoreMemory(config(), MemoryBackend()))
    state = WorkflowState(case_id="HELP-1", summary="Still broken")
    state.metadata["ticket_memory"] = [
        "USER: The error is at https://portal.example.com/login",
        "ASSISTANT: Try https://other.example.com/",
    ]
    state.metadata["similar_cases"] = [{"content": "Another case used https://another.example.com/"}]
    _, body = run._browser_report({"initial_input": {"summary": "Still broken"}, "workflow_state": state})
    assert "https://portal.example.com/login" in body
    assert "https://other.example.com/" not in body
    assert "https://another.example.com/" not in body


class MemoryBackend:
    """In-process test double of the AWS data plane, shared across application instances."""

    def __init__(self):
        self.events = []
        self.records = [
            {
                "namespaces": ["/org/acme/tickets/HELP-2/summary/"],
                "content": {"text": "Login failure; another user tried clearing cache without success."},
                "score": 0.9,
            }
        ]

    def create_event(self, **kwargs):
        if not any(item["clientToken"] == kwargs["clientToken"] for item in self.events):
            self.events.append(kwargs)
        return {}

    def list_events(self, **kwargs):
        events = [
            item
            for item in reversed(self.events)
            if item["actorId"] == kwargs["actorId"] and item["sessionId"] == kwargs["sessionId"]
        ]
        return {"events": events[: kwargs["maxResults"]]}

    def retrieve_memory_records(self, **kwargs):
        assert kwargs["namespacePath"] == "/org/acme/tickets/"
        return {"memoryRecordSummaries": self.records}


def workflow(memory, *, confidence=0.9):
    llm = MagicMock()
    llm.triage_email = AsyncMock(
        return_value=TriageResult(
            service="identity",
            issue_type="access",
            priority="high",
            confidence=confidence,
            missing_info=["browser"] if confidence < 0.6 else [],
            clarifying_questions=["Which browser?"] if confidence < 0.6 else [],
            summary="Login failure",
            source="email",
            sender="user@example.com",
            raw_text="Login failure",
        )
    )
    llm.reflect_triage = AsyncMock(side_effect=lambda result, _subject, _body: result)
    llm.draft_clarification = AsyncMock(return_value="Which browser?")
    result = HelpdeskWorkflow(llm_client=llm, memory=memory)
    result.ticket_flow.create_ticket = AsyncMock(return_value={"status": "created", "ticket_id": "HELP-1"})
    result.ticket_flow.get_ticket = AsyncMock(
        return_value={
            "status": "success",
            "ticket_id": "HELP-1",
            "summary": "Login failure",
            "description": "Original issue",
            "comments": [],
        }
    )
    return result


@pytest.mark.asyncio
async def test_chat_followup_after_restart_reuses_ticket_and_context():
    backend = MemoryBackend()
    memory = AgentCoreMemory(config(), backend)
    first = workflow(memory, confidence=0.3)
    response = await handle_chat_message(first, ConversationStore(), "Login failure", "conversation-1")
    assert response.metadata["current_step"] == "awaiting_clarification"

    second = workflow(AgentCoreMemory(config(), backend))
    followup = await handle_chat_message(
        second, ConversationStore(), "Chrome; cache clearing did not help", "conversation-1"
    )
    second.ticket_flow.create_ticket.assert_not_awaited()
    second.ticket_flow.get_ticket.assert_awaited_once_with("HELP-1")
    assert followup.case_id == "HELP-1"
    snippets = second.llm_client.triage_email.await_args.kwargs["history_snippets"]
    assert any("Which browser?" in snippet for snippet in snippets)
    assert any("OTHER ticket HELP-2" in snippet for snippet in snippets)
    assert followup.metadata["similar_cases"][0]["ticket_id"] == "HELP-2"
    snapshots = [item["payload"][0]["json"]["content"] for item in backend.events if "json" in item["payload"][0]]
    assert all(snapshot["resolution_verified"] is False for snapshot in snapshots)
    assert "Chrome; cache clearing did not help" in snapshots[-1]["report"]


@pytest.mark.asyncio
async def test_email_followup_keeps_clarification_limit_in_jira():
    memory = AgentCoreMemory(config(), MemoryBackend())
    run = workflow(memory, confidence=0.3)
    run.ticket_flow.get_ticket.return_value["comments"] = [
        {"body": "Pending clarifications: Which browser?"},
        {"body": "Pending clarifications: Which URL?"},
    ]
    state = await run.run(
        {
            "subject": "[HELP-1] Login failure",
            "summary": "Still failing",
            "source": "email",
            "metadata": {"message_id": "email-1"},
        }
    )
    assert state.current_step == "diagnosis_required"
    run.llm_client.draft_clarification.assert_not_awaited()
    case = build_case_input(state, None, "HELP-1")
    assert "HELP-2" in case["description_raw"]


@pytest.mark.asyncio
async def test_failed_email_delivery_not_saved_as_assistant_turn():
    backend = MemoryBackend()
    run = workflow(AgentCoreMemory(config(), backend), confidence=0.3)
    run.mail_flow.reply_email = AsyncMock(return_value={"status": "failed", "error": "mail unavailable"})
    await run.run(
        {
            "subject": "[HELP-1] Login failure",
            "summary": "Still failing",
            "source": "email",
            "metadata": {"message_id": "email-1"},
        }
    )
    conversational = [
        item["payload"][0]["conversational"] for item in backend.events if "conversational" in item["payload"][0]
    ]
    assert [item["role"] for item in conversational] == ["USER"]


@pytest.mark.asyncio
async def test_sent_email_response_is_saved_for_the_next_reply():
    backend = MemoryBackend()
    run = workflow(AgentCoreMemory(config(), backend), confidence=0.3)
    run.mail_flow.reply_email = AsyncMock(return_value={"status": "sent"})
    await run.run(
        {
            "subject": "[HELP-1] Login failure",
            "summary": "Still failing",
            "source": "email",
            "metadata": {"message_id": "email-1"},
        }
    )
    history = await run.memory.history("HELP-1", "email-2")
    assert history == ["USER: Still failing", "ASSISTANT: Which browser?"]


@pytest.mark.asyncio
async def test_new_email_saves_original_report_before_acknowledgment():
    backend = MemoryBackend()
    memory = AgentCoreMemory(config(), backend)
    run = workflow(memory, confidence=0.3)
    run.mail_flow.reply_email = AsyncMock(return_value={"status": "sent"})
    await run.run(
        {
            "subject": "Login failure",
            "summary": "Original report",
            "source": "email",
            "metadata": {"message_id": "email-1"},
        }
    )
    history = await memory.history("HELP-1", "email-2")
    assert history[0] == "USER: Original report"
    assert "Thank you for contacting support" in history[1]
    assert history[2] == "ASSISTANT: Which browser?"


@pytest.mark.asyncio
async def test_memory_outage_is_reported_and_jira_workflow_still_runs():
    memory = MagicMock(spec=AgentCoreMemory)
    memory.config = config()
    memory.context_chars = memory.config.context_chars
    memory.history = AsyncMock(side_effect=MemoryError("History unavailable"))
    memory.similar = AsyncMock(side_effect=MemoryError("Search unavailable"))
    memory.save_message = AsyncMock(side_effect=MemoryError("Write unavailable"))
    memory.save_snapshot = AsyncMock(side_effect=MemoryError("Snapshot unavailable"))
    run = workflow(memory)
    state = await run.run({"subject": "[HELP-1] Login failure", "summary": "Still failing", "source": "email"})
    assert state.current_step == "diagnosis_required"
    assert len(state.metadata["memory_errors"]) == 4
    assert all(item["status"] == "failed" for item in state.history if item["step"].startswith("memory"))


@pytest.mark.asyncio
async def test_binding_read_failure_does_not_create_ticket():
    memory = MagicMock(spec=AgentCoreMemory)
    memory.conversation_ticket = AsyncMock(side_effect=MemoryError("Binding unavailable"))
    run = workflow(memory)
    with pytest.raises(MemoryError, match="Binding unavailable"):
        await handle_chat_message(run, ConversationStore(), "Still failing", "conversation-1")
    run.ticket_flow.create_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_snapshot_redacts_nested_values_and_keeps_source():
    backend = MemoryBackend()
    memory = AgentCoreMemory(config(), backend)
    await memory.save_snapshot(
        "HELP-1", {"report": "alice@example.com", "comments": ["password='hidden'"]}, "message-1"
    )
    snapshot = backend.events[0]["payload"][0]["json"]["content"]
    assert snapshot["source_ticket"] == "HELP-1"
    assert "alice@example.com" not in json.dumps(snapshot)
    assert "hidden" not in json.dumps(snapshot)
    assert "extractionMode" not in backend.events[0]
