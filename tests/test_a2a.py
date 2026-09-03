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
