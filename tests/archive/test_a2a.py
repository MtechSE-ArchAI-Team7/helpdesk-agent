"""Tests for the A2A (Agent2Agent) protocol interface."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def _triage_result(confidence, questions=None, missing_info=None):
    from helpdesk_agent.modules.triage.schema import TriageResult

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


def _send_message_payload(text, context_id=None, task_id=None, request_id="req-1"):
    message = {
        "kind": "message",
        "role": "user",
        "messageId": f"msg-{request_id}",
        "parts": [{"kind": "text", "text": text}],
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {"message": message},
    }


class TestA2AInterface(unittest.TestCase):
    def setUp(self) -> None:
        # Mock the LLM client to avoid requiring an API key
        self.mock_patcher = patch("helpdesk_agent.dependencies.get_llm_client")
        mock_get_llm = self.mock_patcher.start()

        self.mock_llm = MagicMock()
        self.mock_llm.triage_email = AsyncMock(return_value=_triage_result(0.9))
        self.mock_llm.draft_clarification = AsyncMock(return_value="Please provide more details.")
        mock_get_llm.return_value = self.mock_llm

        from helpdesk_agent.main import create_app

        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.mock_patcher.stop()

    def test_agent_card_is_served(self) -> None:
        response = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(response.status_code, 200)
        card = response.json()
        self.assertEqual(card["name"], "helpdesk-agent")
        self.assertTrue(card["url"].endswith("/a2a"))
        self.assertFalse(card["capabilities"]["streaming"])
        skill_ids = [s["id"] for s in card["skills"]]
        self.assertIn("triage-helpdesk-request", skill_ids)
        self.assertIn("check-case-status", skill_ids)

    def test_message_send_completes_task(self) -> None:
        response = self.client.post("/a2a", json=_send_message_payload("I need to reset my password"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], "req-1")
        result = body["result"]
        self.assertEqual(result["kind"], "task")
        self.assertEqual(result["status"]["state"], "completed")
        # Agent reply text is attached to the final status message
        reply_text = result["status"]["message"]["parts"][0]["text"]
        self.assertTrue(reply_text)
        # Full ChatResponse payload is exposed as an artifact
        self.assertTrue(result.get("artifacts"))
        artifact_text = result["artifacts"][0]["parts"][0]["text"]
        self.assertIn("conversation_id", artifact_text)

    def test_message_send_multi_turn_clarification(self) -> None:
        # Low confidence triggers the clarification flow -> input-required
        self.mock_llm.triage_email.return_value = _triage_result(
            0.3, questions=["Which system are you locked out of?"], missing_info=["system"]
        )
        first = self.client.post("/a2a", json=_send_message_payload("help me", request_id="req-1")).json()
        result1 = first["result"]
        self.assertEqual(result1["status"]["state"], "input-required")
        context_id = result1["contextId"]
        task_id = result1["id"]
        question_text = result1["status"]["message"]["parts"][0]["text"]
        self.assertIn("Which system", question_text)

        # Follow-up on the same contextId/taskId resumes and completes
        self.mock_llm.triage_email.return_value = _triage_result(0.95)
        second = self.client.post(
            "/a2a",
            json=_send_message_payload("It is the VPN", context_id, task_id, request_id="req-2"),
        ).json()
        result2 = second["result"]
        self.assertEqual(result2["contextId"], context_id)
        self.assertEqual(result2["id"], task_id)
        self.assertEqual(result2["status"]["state"], "completed")

    def test_unknown_method_returns_jsonrpc_error(self) -> None:
        response = self.client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": "req-x", "method": "tasks/doesnotexist", "params": {}},
        )
        body = response.json()
        self.assertIn("error", body)
        self.assertEqual(body["id"], "req-x")


class TestA2AConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        from helpdesk_agent.config import A2AConfig

        config = A2AConfig.from_env()
        self.assertEqual(config.agent_name, "helpdesk-agent")
        self.assertEqual(config.remote_agents, {})

    def test_remote_agents_json_parsing(self) -> None:
        os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENTS"] = '{"jira-bot": "http://localhost:9999"}'
        try:
            from helpdesk_agent.config import A2AConfig

            config = A2AConfig.from_env()
            self.assertEqual(config.remote_agents, {"jira-bot": "http://localhost:9999"})
        finally:
            del os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENTS"]

    def test_remote_agents_invalid_json_raises(self) -> None:
        os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENTS"] = "not-json"
        try:
            from helpdesk_agent.config import A2AConfig

            with self.assertRaises(ValueError):
                A2AConfig.from_env()
        finally:
            del os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENTS"]


class TestA2AClientManager(unittest.TestCase):
    def test_unknown_agent_raises_key_error(self) -> None:
        from helpdesk_agent.config import A2AConfig
        from helpdesk_agent.modules.a2a.client import A2AClientManager

        manager = A2AClientManager(A2AConfig(remote_agents={"jira-bot": "http://localhost:9999"}))
        self.assertEqual(manager.agent_names, ["jira-bot"])
        with self.assertRaises(KeyError):
            asyncio.run(manager.get_agent_card("nope"))


class TestA2AConfigTokensAndArns(unittest.TestCase):
    def test_tokens_and_arns_parsing(self) -> None:
        os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS"] = '{"diagnosis-agent": "tok-123"}'
        os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENT_ARNS"] = (
            '{"diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"}'
        )
        try:
            from helpdesk_agent.config import A2AConfig

            config = A2AConfig.from_env()
            self.assertEqual(config.remote_agent_tokens, {"diagnosis-agent": "tok-123"})
            self.assertEqual(
                config.remote_agent_runtime_arns,
                {"diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"},
            )
        finally:
            del os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS"]
            del os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENT_ARNS"]

    def test_tokens_invalid_json_raises(self) -> None:
        os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS"] = "not-json"
        try:
            from helpdesk_agent.config import A2AConfig

            with self.assertRaises(ValueError):
                A2AConfig.from_env()
        finally:
            del os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS"]

    def test_arns_invalid_shape_raises(self) -> None:
        os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENT_ARNS"] = '["not", "an", "object"]'
        try:
            from helpdesk_agent.config import A2AConfig

            with self.assertRaises(ValueError):
                A2AConfig.from_env()
        finally:
            del os.environ["HELPDESK_AGENT_A2A_REMOTE_AGENT_ARNS"]


class TestA2ASecretArnResolution(unittest.TestCase):
    """HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKEN_SECRET_ARNS resolves via Secrets Manager."""

    def _secret_arn(self) -> str:
        # Built inline so ruff S105 doesn't read it as a hardcoded password.
        return "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:aios/diagnosis-agent/agent-auth-token"

    def _set_env(self, **env):
        for key, value in env.items():
            os.environ[key] = value
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in env])

    def _fake_sm_client(self, secret_string):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": secret_string}
        return client

    def test_pointer_resolves_plain_string_secret(self) -> None:
        self._set_env(
            HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKEN_SECRET_ARNS=f'{{"diagnosis-agent": "{self._secret_arn()}"}}'
        )
        from helpdesk_agent.config import A2AConfig

        with patch("boto3.client", return_value=self._fake_sm_client("real-token-value")) as mock_boto:
            config = A2AConfig.from_env()

        mock_boto.assert_called_once_with("secretsmanager")
        self.assertEqual(config.remote_agent_tokens, {"diagnosis-agent": "real-token-value"})

    def test_pointer_resolves_single_key_json_secret(self) -> None:
        self._set_env(
            HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKEN_SECRET_ARNS=f'{{"diagnosis-agent": "{self._secret_arn()}"}}'
        )
        from helpdesk_agent.config import A2AConfig

        with patch("boto3.client", return_value=self._fake_sm_client('{"token": "real-token-value"}')):
            config = A2AConfig.from_env()

        self.assertEqual(config.remote_agent_tokens, {"diagnosis-agent": "real-token-value"})

    def test_literal_token_takes_precedence_over_pointer(self) -> None:
        self._set_env(
            HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS='{"diagnosis-agent": "literal-token"}',
            HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKEN_SECRET_ARNS=f'{{"diagnosis-agent": "{self._secret_arn()}"}}',
        )
        from helpdesk_agent.config import A2AConfig

        with patch("boto3.client") as mock_boto:
            config = A2AConfig.from_env()

        mock_boto.assert_not_called()
        self.assertEqual(config.remote_agent_tokens, {"diagnosis-agent": "literal-token"})

    def test_no_pointers_means_no_boto3(self) -> None:
        from helpdesk_agent.config import A2AConfig

        with patch("boto3.client") as mock_boto:
            A2AConfig.from_env()

        mock_boto.assert_not_called()

    def test_multi_key_json_secret_rejected(self) -> None:
        self._set_env(
            HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKEN_SECRET_ARNS=f'{{"diagnosis-agent": "{self._secret_arn()}"}}'
        )
        from helpdesk_agent.config import A2AConfig

        with (
            patch("boto3.client", return_value=self._fake_sm_client('{"a": "1", "b": "2"}')),
            self.assertRaises(ValueError),
        ):
            A2AConfig.from_env()


def _case_payload():
    return {
        "case_id": "HELP-1",
        "description_raw": "users cannot log in",
        "affected_services": ["auth-service"],
        "time_window": {"start": "2026-09-18T10:00:00+00:00", "end": "2026-09-18T11:00:00+00:00"},
        "requester": {"id": "u-1", "display_name": "Jane"},
        "classification": {"category": "incident", "impact": "high", "urgency": "high", "priority": "high"},
        "jira_issue_key": "HELP-1",
    }


def _fake_boto_client(captured, response=None):
    """Build a boto3 bedrock-agentcore client stand-in.

    Provides the ``meta.events.register`` surface that ``_send_via_agentcore``
    uses for its before-sign header hook, records invoke kwargs into
    ``captured`` (plus a ``client`` back-reference), and returns ``response``
    (default: an empty dict, i.e. no response body to parse).
    """
    from types import SimpleNamespace

    class FakeEvents:
        def __init__(self) -> None:
            self.registered: list = []

        def register(self, name, fn):
            self.registered.append((name, fn))

    class FakeBotoClient:
        def __init__(self) -> None:
            self.meta = SimpleNamespace(events=FakeEvents())

        def invoke_agent_runtime(self, **kwargs):
            captured.update(kwargs)
            captured["client"] = self
            return response if response is not None else {}

    return FakeBotoClient()


class TestSendCase(unittest.TestCase):
    def _manager(self, **overrides):
        from helpdesk_agent.config import A2AConfig
        from helpdesk_agent.modules.a2a.client import A2AClientManager

        defaults = {
            "remote_agents": {"diagnosis-agent": "http://localhost:9000"},
            "remote_agent_tokens": {"diagnosis-agent": "tok-123"},
        }
        defaults.update(overrides)
        config = A2AConfig(**defaults)
        return A2AClientManager(config)

    def test_unknown_agent_raises_key_error(self) -> None:
        manager = self._manager()
        with self.assertRaises(KeyError):
            asyncio.run(manager.send_case("nope", _case_payload()))

    def test_http_transport_posts_envelope_with_headers(self) -> None:
        manager = self._manager()
        captured = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"jsonrpc": "2.0", "id": "x", "result": {"task": {"id": "server-task-1"}}}

        class FakeHttpxClient:
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return FakeResponse()

        manager._httpx_client = FakeHttpxClient()
        manager._factory = object()  # sentinel: skip real factory construction

        outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload(), context_id="HELP-1"))

        self.assertTrue(outcome.submitted)
        # The server assigns the task id; the client adopts it from the response.
        self.assertEqual(outcome.task_id, "server-task-1")
        self.assertEqual(captured["url"], "http://localhost:9000/")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer tok-123")
        self.assertEqual(captured["headers"]["A2A-Version"], "1.0")

        envelope = captured["json"]
        self.assertEqual(envelope["method"], "SendMessage")
        message = envelope["params"]["message"]
        self.assertEqual(message["role"], "ROLE_USER")
        # A new submission carries no taskId/contextId: an inbound taskId
        # means "resume", and an unknown one is rejected as "Task not found".
        self.assertNotIn("taskId", message)
        self.assertNotIn("contextId", message)
        self.assertEqual(message["parts"][0]["data"]["case_id"], "HELP-1")

    def test_http_transport_failure_is_not_raised(self) -> None:
        import httpx

        manager = self._manager()

        class FailingHttpxClient:
            async def post(self, url, json=None, headers=None):
                raise httpx.ConnectError("connection refused")

        manager._httpx_client = FailingHttpxClient()
        manager._factory = object()

        outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload()))
        self.assertFalse(outcome.submitted)
        self.assertIn("transport error", outcome.reason)

    def test_http_transport_401_is_graceful(self) -> None:
        manager = self._manager()

        class UnauthorizedResponse:
            status_code = 401

        class FakeHttpxClient:
            async def post(self, url, json=None, headers=None):
                return UnauthorizedResponse()

        manager._httpx_client = FakeHttpxClient()
        manager._factory = object()

        outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload()))
        self.assertFalse(outcome.submitted)
        self.assertIn("401", outcome.reason)

    def test_agentcore_transport_uses_runtime_arn(self) -> None:
        from helpdesk_agent.config import A2AConfig
        from helpdesk_agent.modules.a2a.client import A2AClientManager

        manager = A2AClientManager(
            A2AConfig(
                remote_agent_runtime_arns={
                    "diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
                },
                remote_agent_tokens={"diagnosis-agent": "tok-123"},
            )
        )
        captured = {}

        with patch("boto3.client", return_value=_fake_boto_client(captured)) as mock_boto:
            outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload(), context_id="HELP-1"))

        self.assertTrue(outcome.submitted)
        self.assertIsNotNone(outcome.run_id)
        mock_boto.assert_called_once()
        self.assertEqual(mock_boto.call_args.args[0], "bedrock-agentcore")
        self.assertEqual(
            captured["agentRuntimeArn"], "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
        )
        self.assertEqual(captured["runtimeSessionId"], outcome.run_id)
        self.assertEqual(captured["qualifier"], "DEFAULT")
        import json as _json

        envelope = _json.loads(captured["payload"].decode("utf-8"))
        self.assertEqual(envelope["method"], "SendMessage")
        self.assertEqual(envelope["params"]["message"]["parts"][0]["data"]["case_id"], "HELP-1")

    def test_agentcore_adopts_server_assigned_task_id(self) -> None:
        import io
        import json as _json

        manager = self._manager(
            remote_agent_runtime_arns={
                "diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
            }
        )
        body = _json.dumps(
            {"jsonrpc": "2.0", "id": "x", "result": {"task": {"id": "srv-task-9", "contextId": "ctx-9"}}}
        ).encode("utf-8")
        captured = {}

        with patch("boto3.client", return_value=_fake_boto_client(captured, response={"response": io.BytesIO(body)})):
            outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload()))

        self.assertTrue(outcome.submitted)
        self.assertEqual(outcome.task_id, "srv-task-9")
        envelope = _json.loads(captured["payload"].decode("utf-8"))
        message = envelope["params"]["message"]
        self.assertNotIn("taskId", message)
        self.assertNotIn("contextId", message)

    def test_agentcore_session_id_is_deterministic_per_content(self) -> None:
        manager = self._manager(
            remote_agent_runtime_arns={
                "diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
            }
        )

        # Same envelope content -> same session id. Patch uuid4 (used for the
        # messageId and request id) to pin the envelope content.
        with (
            patch("boto3.client", return_value=_fake_boto_client({})),
            patch("helpdesk_agent.modules.a2a.diagnosis.uuid4", return_value=MagicMock(hex="fixed-uuid")),
        ):
            first = asyncio.run(manager.send_case("diagnosis-agent", _case_payload(), context_id="ctx"))
            second = asyncio.run(manager.send_case("diagnosis-agent", _case_payload(), context_id="ctx"))
        self.assertEqual(first.run_id, second.run_id)

    def test_agentcore_read_timeout_counts_as_submitted(self) -> None:
        manager = self._manager(
            remote_agent_runtime_arns={
                "diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
            }
        )

        class TimeoutBotoClient:
            def __init__(self) -> None:
                from types import SimpleNamespace

                self.meta = SimpleNamespace(events=SimpleNamespace(register=lambda *a, **k: None))

            def invoke_agent_runtime(self, **kwargs):
                from botocore.exceptions import ReadTimeoutError

                raise ReadTimeoutError(endpoint_url="https://example.com")

        with patch("boto3.client", return_value=TimeoutBotoClient()):
            outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload()))

        self.assertTrue(outcome.submitted)
        self.assertEqual(outcome.reason, "submitted; response not awaited")

    def test_agentcore_registers_auth_and_version_headers(self) -> None:
        manager = self._manager(
            remote_agent_runtime_arns={
                "diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
            }
        )
        captured = {}
        fake_client = _fake_boto_client(captured)

        with patch("boto3.client", return_value=fake_client):
            outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload()))

        self.assertTrue(outcome.submitted)
        registered = fake_client.meta.events.registered
        self.assertEqual(len(registered), 1)
        event_name, hook = registered[0]
        self.assertEqual(event_name, "before-sign.bedrock-agentcore.InvokeAgentRuntime")

        # Use a real botocore AWSRequest: its headers object (HTTPHeaders)
        # intentionally lacks dict-style .update(), which a plain dict stand-in
        # would not catch.
        from botocore.awsrequest import AWSRequest

        request = AWSRequest(method="POST", url="https://bedrock-agentcore.example.com/", data=b"{}")
        hook(request=request)
        self.assertEqual(request.headers["A2A-Version"], "1.0")
        self.assertEqual(
            request.headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Authorization"],
            "Bearer tok-123",
        )
        # The standard Authorization header must stay unused: SigV4 owns it.
        self.assertNotIn("Authorization", request.headers)

    def test_agentcore_disables_botocore_retries(self) -> None:
        manager = self._manager(
            remote_agent_runtime_arns={
                "diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
            }
        )

        with patch("boto3.client", return_value=_fake_boto_client({})) as mock_boto:
            asyncio.run(manager.send_case("diagnosis-agent", _case_payload()))

        config = mock_boto.call_args.kwargs["config"]
        self.assertEqual(config.retries.get("total_max_attempts"), 1)

    def test_agentcore_missing_token_fails_fast(self) -> None:
        manager = self._manager(
            remote_agent_runtime_arns={
                "diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
            },
            remote_agent_tokens={},
        )

        with patch("boto3.client") as mock_boto:
            outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload()))

        self.assertFalse(outcome.submitted)
        self.assertIn("token", outcome.reason)
        mock_boto.assert_not_called()

    def test_agentcore_jsonrpc_error_is_failure(self) -> None:
        import io
        import json as _json

        manager = self._manager(
            remote_agent_runtime_arns={
                "diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"
            }
        )
        body = _json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "x",
                "error": {"code": -32009, "message": "A2A version '0.3' is not supported by this handler"},
            }
        ).encode("utf-8")
        captured = {}

        with patch("boto3.client", return_value=_fake_boto_client(captured, response={"response": io.BytesIO(body)})):
            outcome = asyncio.run(manager.send_case("diagnosis-agent", _case_payload()))

        self.assertFalse(outcome.submitted)
        self.assertIn("JSON-RPC error", outcome.reason)
        self.assertIn("not supported", outcome.reason)


class TestDiagnoseHandoff(unittest.TestCase):
    """Workflow diagnose node triggers the fire-and-forget hand-off."""

    def _state(self):
        from helpdesk_agent.models import WorkflowState

        workflow_state = WorkflowState(case_id="case-1", summary="users cannot log in", service="auth-service")
        return {
            "workflow_state": workflow_state,
            "initial_input": {},
            "triage_result": _triage_result(0.9),
            "faq_results": None,
            "ticket_id": "HELP-1",
            "existing_ticket": None,
        }

    def _workflow(self, manager):
        from helpdesk_agent.core.workflow import HelpdeskWorkflow

        workflow = HelpdeskWorkflow(llm_client=MagicMock(), a2a_client_manager=manager)
        workflow.ticket_flow = MagicMock()
        workflow.ticket_flow.add_comment = AsyncMock(return_value={"status": "success"})
        return workflow

    def test_diagnose_triggers_send_case(self) -> None:
        from helpdesk_agent.modules.a2a.client import DeliveryOutcome

        manager = MagicMock()
        manager.has_target = MagicMock(return_value=True)
        manager.send_case = AsyncMock(return_value=DeliveryOutcome(submitted=True, task_id="t-1"))

        workflow = self._workflow(manager)
        state = asyncio.run(workflow._diagnose_node(self._state()))

        manager.send_case.assert_awaited_once()
        agent_name, payload = manager.send_case.await_args.args[:2]
        self.assertEqual(agent_name, "diagnosis-agent")
        self.assertEqual(payload["case_id"], "HELP-1")
        self.assertEqual(payload["jira_issue_key"], "HELP-1")
        self.assertEqual(payload["affected_services"], ["auth-service"])
        self.assertEqual(payload["description_raw"], "users cannot log in")
        self.assertEqual(manager.send_case.await_args.kwargs["context_id"], "case-1")

        ws = state["workflow_state"]
        self.assertEqual(ws.current_step, "diagnosis_required")
        self.assertTrue(ws.metadata["diagnosis_trigger"]["submitted"])

    def test_diagnose_skips_when_agent_not_configured(self) -> None:
        manager = MagicMock()
        manager.has_target = MagicMock(return_value=False)
        manager.send_case = AsyncMock()

        workflow = self._workflow(manager)
        state = asyncio.run(workflow._diagnose_node(self._state()))

        manager.send_case.assert_not_awaited()
        ws = state["workflow_state"]
        self.assertEqual(ws.current_step, "diagnosis_required")
        self.assertNotIn("diagnosis_trigger", ws.metadata)

    def test_diagnose_without_manager(self) -> None:
        workflow = self._workflow(None)
        state = asyncio.run(workflow._diagnose_node(self._state()))
        self.assertEqual(state["workflow_state"].current_step, "diagnosis_required")

    def test_diagnose_handoff_failure_degrades_gracefully(self) -> None:
        from helpdesk_agent.modules.a2a.client import DeliveryOutcome

        manager = MagicMock()
        manager.has_target = MagicMock(return_value=True)
        manager.send_case = AsyncMock(return_value=DeliveryOutcome(submitted=False, reason="remote returned HTTP 401"))

        workflow = self._workflow(manager)
        state = asyncio.run(workflow._diagnose_node(self._state()))

        ws = state["workflow_state"]
        self.assertEqual(ws.current_step, "diagnosis_required")
        self.assertFalse(ws.metadata["diagnosis_trigger"]["submitted"])
        # Failure is surfaced on the ticket as an extra comment
        comment_bodies = [call.args[1] for call in workflow.ticket_flow.add_comment.await_args_list]
        self.assertTrue(any("unreachable" in body for body in comment_bodies))

    def test_diagnose_handoff_exception_never_raises(self) -> None:
        manager = MagicMock()
        manager.has_target = MagicMock(return_value=True)
        manager.send_case = AsyncMock(side_effect=RuntimeError("boom"))

        workflow = self._workflow(manager)
        state = asyncio.run(workflow._diagnose_node(self._state()))

        ws = state["workflow_state"]
        self.assertEqual(ws.current_step, "diagnosis_required")
        self.assertFalse(ws.metadata["diagnosis_trigger"]["submitted"])
