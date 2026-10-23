import base64
import importlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

import helpdesk_agent.api.app as main
import helpdesk_agent.api.dependencies as dependencies
from helpdesk_agent import cli, memory_setup
from helpdesk_agent.api.routes.email import email_webhook
from helpdesk_agent.api.routes.ticketing import JiraMemoryWebhook, jira_memory_webhook
from helpdesk_agent.api.schemas.email import EmailWebhook
from helpdesk_agent.application.chat import handle_chat_message
from helpdesk_agent.config.settings import A2AConfig, LLMConfig, MCPConfig, MemoryConfig
from helpdesk_agent.domain.models.chat_response import ChatMessage
from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.infrastructure.memory.agentcore import MemoryError
from helpdesk_agent.infrastructure.persistence.conversations import ConversationStore
from helpdesk_agent.modules.chat import router as chat_router

pytestmark = pytest.mark.unit


@pytest.fixture
def workflow():
    flow = MagicMock()
    flow.memory = None
    flow.project_key = "HELP"
    flow.run = AsyncMock(return_value=WorkflowState(case_id="HELP-1", summary="Help"))
    flow.save_chat_response = AsyncMock()
    return flow


@pytest.mark.asyncio
async def test_email_router_maps_request_fields(workflow):
    payload = EmailWebhook(sender="u@example.com", subject="  ", body=" Body ", message_id="123")
    result = await email_webhook(payload, workflow)
    assert result["case_id"] == "HELP-1"
    request = workflow.run.await_args.args[0]
    assert request["summary"] == "Email request\n\nBody"
    assert request["case_id"] == "123"
    assert request["metadata"]["message_id"] == "123"
    await email_webhook(EmailWebhook(sender="u@example.com", subject="Help", body="Body"), workflow)
    assert workflow.run.await_args.args[0]["case_id"].startswith("email-")


@pytest.mark.asyncio
async def test_conversation_router_crud(workflow):
    store = ConversationStore()
    result = await chat_router.chat(ChatMessage(message="Help"), workflow, store)
    conversation = await chat_router.get_conversation(result.conversation_id, store)
    assert conversation["messages"][0]["content"] == "Help"
    assert (await chat_router.list_conversations(store))["count"] == 1
    assert (await chat_router.delete_conversation(result.conversation_id, store))["status"] == "deleted"
    for method in (chat_router.get_conversation, chat_router.delete_conversation):
        with pytest.raises(HTTPException) as error:
            await method(result.conversation_id, store)
        assert error.value.status_code == 404


@pytest.mark.parametrize(
    "step,metadata,clarification,matches,text",
    [
        (
            "email_retrieved",
            {"email_messages": [{"subject": "Help", "from": "u@example.com"}, {}]},
            None,
            [],
            "2. No Subject",
        ),
        ("email_retrieved", {}, None, [], "No unread emails"),
        ("awaiting_clarification", {}, {}, [], "Could you describe"),
        (
            "awaiting_clarification",
            {},
            {"questions": ["Which browser?"], "missing_info": ["browser"]},
            [],
            "Missing information: browser",
        ),
        ("faq_answer_provided", {}, None, [{"title": "Login", "body": "Reset"}], "**Login**"),
        ("faq_answer_provided", {}, None, [], "knowledge base"),
        ("diagnosis_required", {}, None, [], "escalated"),
    ],
)
@pytest.mark.asyncio
async def test_chat_response_formats_each_terminal_state(workflow, step, metadata, clarification, matches, text):
    workflow.run.return_value = WorkflowState(
        case_id="HELP-1",
        summary="Help",
        current_step=step,
        metadata=metadata,
        clarification=clarification,
        faq_matches=matches,
        ticket={"ticket_id": "HELP-1"},
    )
    result = await handle_chat_message(workflow, ConversationStore(), "Help")
    assert text in result.agent_response
    assert result.metadata["ticket_id"] == "HELP-1"
    workflow.save_chat_response.assert_awaited_once()


@pytest.mark.parametrize(
    "case", ["disabled", "wrong_project", "mcp_disabled", "fetch_failed", "save_failed", "success"]
)
@pytest.mark.asyncio
async def test_jira_memory_webhook_validates_and_refetches(workflow, case):
    workflow.memory = MagicMock()
    workflow.memory.save_snapshot = AsyncMock()
    workflow.ticket_flow.get_ticket = AsyncMock(return_value={"status": "success", "summary": "Trusted Jira"})
    ticket_id = "HELP-1"
    expected = None
    if case == "disabled":
        workflow.memory = None
        expected = 503
    elif case == "wrong_project":
        ticket_id = "OPS-1"
        expected = 400
    elif case == "mcp_disabled":
        workflow.mcp_adapter = None
        workflow.mcp_enabled = False
        expected = 503
    elif case == "fetch_failed":
        workflow.ticket_flow.get_ticket.return_value = {"status": "failed"}
        expected = 502
    elif case == "save_failed":
        workflow.memory.save_snapshot.side_effect = MemoryError("Storage unavailable")
        expected = 502
    payload = JiraMemoryWebhook.model_validate({"issue": {"key": ticket_id}})
    if expected:
        with pytest.raises(HTTPException) as error:
            await jira_memory_webhook(payload, workflow)
        assert error.value.status_code == expected
    else:
        result = await jira_memory_webhook(payload, workflow)
        assert result == {"ticket_id": "HELP-1", "status": "submitted", "extraction": "asynchronous"}
        args = workflow.memory.save_snapshot.await_args.args
        assert args[1]["snapshot_origin"] == "jira_update"
        assert args[2].startswith("jira-update:")


def test_dependency_factories_and_caching(monkeypatch):
    dependencies.get_llm_config.cache_clear()
    dependencies.get_conversation_store.cache_clear()
    monkeypatch.setenv("HELPDESK_AGENT_USE_MCP", "1")
    with (
        patch.object(dependencies.MCPConfig, "from_env", return_value=MCPConfig()),
        patch.object(dependencies.LLMConfig, "from_env", return_value=LLMConfig(api_key="test")),
        patch.object(dependencies.A2AConfig, "from_env", return_value=A2AConfig()),
        patch.object(dependencies.MemoryConfig, "from_env", return_value=MemoryConfig()),
        patch.object(dependencies, "MCPProxyAdapter") as adapter,
        patch.object(dependencies, "LLMTriageClient") as llm,
        patch.object(dependencies, "A2AClientManager") as manager,
        patch.object(dependencies, "HelpdeskWorkflow") as constructor,
    ):
        dependencies.get_workflow()
        constructor.assert_called_once_with(
            adapter.return_value, llm.return_value, a2a_client_manager=manager.return_value, memory=None
        )
        assert dependencies.get_conversation_store() is dependencies.get_conversation_store()
        assert dependencies.get_llm_config().api_key == "test"
    dependencies.get_memory.cache_clear()
    with (
        patch.object(dependencies.MemoryConfig, "from_env", return_value=SimpleNamespace(enabled=True)),
        patch.object(dependencies, "AgentCoreMemory") as memory,
    ):
        assert dependencies.get_memory() is memory.return_value
    dependencies.get_llm_config.cache_clear()
    dependencies.get_conversation_store.cache_clear()


def test_cli_passes_arguments_and_prints_result(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["helpdesk-agent", "--case-id", "case-42", "--summary", "Issue", "--source", "slack"]
    )
    with patch.object(cli, "HelpdeskWorkflow") as constructor:
        constructor.return_value.run = AsyncMock(return_value=WorkflowState(case_id="case-42", summary="Issue"))
        cli.main()
        assert constructor.return_value.run.await_args.args[0]["source"] == "slack"
        assert "'case_id': 'case-42'" in capsys.readouterr().out


def test_server_main_uses_configured_port(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    with patch.object(main.uvicorn, "run") as run:
        main.main()
    run.assert_called_once_with("helpdesk_agent.main:create_app", host="0.0.0.0", port=9000, factory=True)


@pytest.mark.asyncio
async def test_app_factory_health_invocations_and_lifespan(workflow):
    with (
        patch.object(main, "get_llm_config", return_value=LLMConfig(api_key="test")),
        patch.object(main, "get_workflow", return_value=workflow),
        patch.object(main, "get_a2a_config", return_value=A2AConfig()),
        patch.object(main, "setup_observability"),
        patch.object(main, "flush_observability") as flush,
        patch.object(main, "email_webhook", new_callable=AsyncMock, return_value={"status": "email"}) as email,
        patch.object(main, "jira_memory_webhook", new_callable=AsyncMock, return_value={"status": "jira"}) as jira,
    ):
        app = main.create_app()
        routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
        assert (await routes["/"]())["message"] == "helpdesk agent is running"
        assert await routes["/healthz"]() == {"status": "healthy"}
        assert await routes["/ping"]() == {"status": "Healthy"}
        assert await routes["/invocations"](
            {"sender": "u@example.com", "subject": "Help", "body": "Body"}, workflow
        ) == {"status": "email"}
        assert await routes["/invocations"](
            {"path": "/webhooks/jira", "payload": {"issue": {"key": "HELP-1"}}}, workflow
        ) == {"status": "jira"}
        email.assert_awaited_once()
        jira.assert_awaited_once()
        with pytest.raises(HTTPException) as error:
            await routes["/invocations"]({"path": "/unsupported"}, workflow)
        assert error.value.status_code == 404
        async with main._lifespan(app):
            flush.assert_not_called()
        flush.assert_called_once()
    with patch.object(main, "get_llm_config", return_value=LLMConfig()):
        with pytest.raises(RuntimeError, match="API_KEY"):
            main.create_app()


@pytest.mark.parametrize("outcome", ["active", "failed", "timeout"])
@pytest.mark.parametrize("kms", [False, True])
def test_memory_provisioning_statuses(monkeypatch, capsys, outcome, kms):
    arguments = ["memory-create", "--name", "Helpdesk", "--region", "us-west-2"]
    if kms:
        arguments += ["--kms-key-arn", "test-key"]
    monkeypatch.setattr("sys.argv", arguments)
    client = MagicMock()
    client.create_memory.return_value = {"memory": {"id": "memory-1", "status": "CREATING"}}
    client.get_memory.side_effect = [
        {"memory": {"id": "memory-1", "status": "CREATING"}},
        {
            "memory": {
                "id": "memory-1",
                "status": "FAILED" if outcome == "failed" else "ACTIVE",
                "strategies": [{"status": "ACTIVE"}],
            }
        },
    ]
    clock = [0, 0, 301] if outcome == "timeout" else [0, 0, 1]
    with (
        patch.object(memory_setup.boto3, "client", return_value=client),
        patch.object(memory_setup.time, "monotonic", side_effect=clock),
        patch.object(memory_setup.time, "sleep"),
    ):
        if outcome == "active":
            memory_setup.main()
            assert "HELPDESK_AGENT_MEMORY_ID=memory-1" in capsys.readouterr().out
        else:
            with pytest.raises(RuntimeError, match="failed" if outcome == "failed" else "not active"):
                memory_setup.main()
    assert ("encryptionKeyArn" in client.create_memory.call_args.kwargs) is kms


@pytest.mark.parametrize("days", [2, 366])
def test_memory_provisioning_rejects_invalid_retention(monkeypatch, days):
    monkeypatch.setattr(
        "sys.argv", ["memory-create", "--name", "Helpdesk", "--region", "us-west-2", "--retention-days", str(days)]
    )
    with patch.object(memory_setup.boto3, "client") as client:
        with pytest.raises(SystemExit):
            memory_setup.main()
        client.assert_not_called()


@pytest.fixture
def handler(monkeypatch):
    with patch("boto3.client"):
        module = importlib.import_module("helpdesk_agent.api.lambda_handler")
    monkeypatch.setattr(module, "_client", MagicMock())
    monkeypatch.setattr(module, "AGENT_RUNTIME_ARN", "test-runtime")
    monkeypatch.setattr(module, "WEBHOOK_SECRET", "")
    return module


@pytest.mark.parametrize(
    "event,status",
    [
        ({"body": "invalid"}, 400),
        ({"body": "[]"}, 400),
        ({}, 400),
        ({"body": "{}"}, 400),
        ({"body": "{}", "rawPath": "/webhooks/jira"}, 400),
        ({"body": '{"issue":{"key":12}}', "path": "/webhooks/jira"}, 400),
        ({"body": "{}", "path": "/unknown"}, 404),
        ({"body": "/w==", "isBase64Encoded": True}, 400),
    ],
)
def test_lambda_rejects_invalid_requests(handler, event, status):
    assert handler.lambda_handler(event, None)["statusCode"] == status
    handler._client.invoke_agent_runtime.assert_not_called()


def test_lambda_requires_configuration_and_secret(handler, monkeypatch):
    monkeypatch.setattr(handler, "AGENT_RUNTIME_ARN", "")
    assert handler.lambda_handler({}, None)["statusCode"] == 500
    monkeypatch.setattr(handler, "AGENT_RUNTIME_ARN", "test-runtime")
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", "test-value")
    assert handler.lambda_handler({"headers": None}, None)["statusCode"] == 401
    assert handler._lower_headers({"headers": {"X-Webhook-Secret": "test-value"}}) == {"x-webhook-secret": "test-value"}


@pytest.mark.parametrize(
    "payload,path",
    [
        ({"sender": "u@example.com", "subject": "Help", "body": "Body", "message_id": "123"}, "/webhooks/email"),
        ({"from_email": "u@example.com", "subject": "Help", "body_plain": "Body"}, "/webhooks/email"),
        ({"fromAddress": "u@example.com", "messageId": 123}, "/webhooks/email"),
        ({"issue": {"key": "HELP-1"}}, "/webhooks/jira"),
    ],
)
def test_lambda_forwards_valid_payloads_and_stable_session(handler, payload, path):
    handler._client.invoke_agent_runtime.return_value = {"response": io.BytesIO(b'{"status":"ok"}')}
    body = base64.b64encode(json.dumps(payload).encode()).decode()
    result = handler.lambda_handler({"body": body, "isBase64Encoded": True, "rawPath": path}, None)
    assert json.loads(result["body"]) == {"status": "ok"}
    call = handler._client.invoke_agent_runtime.call_args.kwargs
    assert json.loads(call["payload"]) == {"path": path, "payload": payload}
    assert len(call["runtimeSessionId"]) >= 33
    if "message_id" in payload or "messageId" in payload or "issue" in payload:
        assert handler._session_id(payload) == handler._session_id(payload)
    assert handler._session_id({"in_reply_to": "123"}) == handler._session_id({"message_id": "123"})


@pytest.mark.parametrize(
    "code,status",
    [("ThrottlingException", 504), ("ServiceQuotaExceededException", 504), ("AccessDeniedException", 502)],
)
def test_lambda_reports_invocation_errors(handler, code, status):
    handler._client.invoke_agent_runtime.side_effect = ClientError({"Error": {"Code": code}}, "InvokeAgentRuntime")
    result = handler.lambda_handler({"body": '{"fromAddress":"u@example.com"}'}, None)
    assert result["statusCode"] == status
    assert json.loads(result["body"])["detail"] == code


def test_lambda_reads_event_stream_and_non_json(handler):
    stream = MagicMock()
    stream.iter_lines.return_value = [b"event: message", b'data: {"status":', b'data: "ok"}']
    assert handler._read_agent_response({"response": stream, "contentType": "text/event-stream"}) == {"status": "ok"}
    assert handler._read_agent_response({"response": io.BytesIO(b"plain text")}) == {"raw": "plain text"}
