import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from helpdesk_agent.api.app import create_app
from helpdesk_agent.api.dependencies import get_workflow
from helpdesk_agent.domain.exceptions import MemoryError


@pytest.fixture
def webhook():
    workflow = MagicMock()
    workflow.project_key = "HELP"
    workflow.memory.save_snapshot = AsyncMock()
    workflow.ticket_flow.get_ticket = AsyncMock(
        return_value={
            "status": "success",
            "ticket_id": "HELP-1",
            "summary": "Login failures",
            "description": "Users receive callback errors",
            "comments": [{"body": "Corrected callback URL; user testing pending", "author": "private-user"}],
            "issue_status": "In Progress",
            "resolution": None,
        }
    )
    with patch("helpdesk_agent.api.dependencies.get_llm_client"):
        app = create_app()
    app.dependency_overrides[get_workflow] = lambda: workflow
    with TestClient(app) as client:
        yield client, workflow


def test_jira_updates_refetch_and_ingest_troubleshooting(webhook):
    client, workflow = webhook
    payload = {
        "issue": {"key": "HELP-1", "description": "Do not trust webhook text"},
        "webhookEvent": "jira:issue_updated",
    }
    for _ in range(2):
        response = client.post("/webhooks/jira", json=payload)
        assert response.status_code == 200
        assert response.json() == {"ticket_id": "HELP-1", "status": "submitted", "extraction": "asynchronous"}
    workflow.ticket_flow.get_ticket.assert_awaited_with("HELP-1")
    first, second = workflow.memory.save_snapshot.await_args_list
    assert first == second
    snapshot = first.args[1]
    assert snapshot["ticket_description"] == "Users receive callback errors"
    assert snapshot["ticket_comments"] == ["Corrected callback URL; user testing pending"]
    assert snapshot["resolution_verified"] is False


@pytest.mark.parametrize(
    "failure,expected_status",
    [("disabled", 503), ("mock_jira", 503), ("wrong_project", 400), ("jira", 502), ("memory", 502)],
)
def test_jira_update_failures_are_explicit(webhook, failure, expected_status):
    client, workflow = webhook
    ticket = "HELP-1"
    if failure == "disabled":
        workflow.memory = None
    elif failure == "mock_jira":
        workflow.mcp_adapter = None
    elif failure == "wrong_project":
        ticket = "OTHER-1"
    elif failure == "jira":
        workflow.ticket_flow.get_ticket.return_value = {"status": "failed"}
    elif failure == "memory":
        workflow.memory.save_snapshot.side_effect = MemoryError("Snapshot unavailable")
    response = client.post("/webhooks/jira", json={"issue": {"key": ticket}})
    assert response.status_code == expected_status
    assert "detail" in response.json()


def test_agentcore_invocations_support_jira_updates(webhook):
    client, workflow = webhook
    response = client.post("/invocations", json={"path": "/webhooks/jira", "payload": {"issue": {"key": "HELP-1"}}})
    assert response.status_code == 200
    workflow.memory.save_snapshot.assert_awaited_once()
    assert client.post("/invocations", json={"path": "/unsupported"}).status_code == 404


def test_lambda_forwards_jira_and_preserves_email_contract(monkeypatch):
    import importlib
    import io

    with patch("boto3.client"):
        handler = importlib.import_module("helpdesk_agent.api.lambda_handler")
    monkeypatch.setattr(handler, "AGENT_RUNTIME_ARN", "runtime-arn")
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", "test-webhook-secret")
    client = MagicMock()
    monkeypatch.setattr(handler, "_client", client)

    def response(**kwargs):
        return {"response": io.BytesIO(b'{"status":"submitted"}'), "contentType": "application/json"}

    client.invoke_agent_runtime.side_effect = response
    jira = {"issue": {"key": "HELP-1"}}
    email = {"sender": "user@example.com", "subject": "Login failure", "body": "Details"}
    for path, payload in [("/webhooks/jira", jira), ("/webhooks/email", email)]:
        event = {"rawPath": path, "headers": {"x-webhook-secret": "test-webhook-secret"}, "body": json.dumps(payload)}
        assert handler.lambda_handler(event, None)["statusCode"] == 200
        arguments = client.invoke_agent_runtime.call_args.kwargs
        assert json.loads(arguments["payload"]) == {"path": path, "payload": payload}
        assert len(arguments["runtimeSessionId"]) >= 33
    assert handler.lambda_handler({"body": json.dumps(jira)}, None)["statusCode"] == 401
    bad_event = {"rawPath": "/webhooks/jira", "headers": {"x-webhook-secret": "test-webhook-secret"}, "body": "{}"}
    assert handler.lambda_handler(bad_event, None)["statusCode"] == 400


def test_provisioning_creates_ticket_scoped_strategies(monkeypatch, capsys):
    from botocore.session import get_session
    from botocore.validate import validate_parameters

    from helpdesk_agent.infrastructure.memory.agentcore import FACTS_NAMESPACE, SUMMARY_NAMESPACE
    from helpdesk_agent.memory_setup import main

    monkeypatch.setattr("sys.argv", ["helpdesk-memory-create", "--name", "HelpdeskMemory", "--region", "us-west-2"])
    client = MagicMock()
    client.create_memory.return_value = {"memory": {"id": "HelpdeskMemory-abcdefghij", "status": "CREATING"}}
    client.get_memory.return_value = {
        "memory": {"id": "HelpdeskMemory-abcdefghij", "status": "ACTIVE", "strategies": [{"status": "ACTIVE"}]}
    }
    with patch("helpdesk_agent.memory_setup.boto3.client", return_value=client):
        main()
    arguments = client.create_memory.call_args.kwargs
    model = get_session().get_service_model("bedrock-agentcore-control").operation_model("CreateMemory")
    validate_parameters(arguments, model.input_shape)
    assert arguments["eventExpiryDuration"] == 90
    assert arguments["memoryStrategies"] == [
        {"summaryMemoryStrategy": {"name": "TicketSummary", "namespaceTemplates": [SUMMARY_NAMESPACE]}},
        {"semanticMemoryStrategy": {"name": "TicketFacts", "namespaceTemplates": [FACTS_NAMESPACE]}},
    ]
    assert "HELPDESK_AGENT_MEMORY_ID=HelpdeskMemory-abcdefghij" in capsys.readouterr().out
