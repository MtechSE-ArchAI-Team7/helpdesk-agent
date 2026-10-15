import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


class TestChatEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        # Mock the LLM client to avoid requiring API key
        self.mock_patcher = patch("helpdesk_agent.dependencies.get_llm_client")
        mock_get_llm = self.mock_patcher.start()

        from helpdesk_agent.modules.triage.schema import TriageResult

        mock_llm = MagicMock()
        mock_llm.triage_email = AsyncMock(
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
        mock_get_llm.return_value = mock_llm

        from helpdesk_agent.main import create_app

        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.mock_patcher.stop()

    def test_chat_creates_new_conversation(self) -> None:
        response = self.client.post(
            "/chat",
            json={"message": "I need help resetting my password"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("conversation_id", data)
        self.assertIn("agent_response", data)
        self.assertIn("case_id", data)
        self.assertIsNotNone(data["conversation_id"])

    def test_chat_continues_conversation(self) -> None:
        # Create initial chat
        response1 = self.client.post(
            "/chat",
            json={"message": "I need help resetting my password"},
        )
        conv_id = response1.json()["conversation_id"]

        # Continue conversation
        response2 = self.client.post(
            "/chat",
            json={"message": "I've tried the reset link but it expired", "conversation_id": conv_id},
        )
        self.assertEqual(response2.status_code, 200)
        data = response2.json()
        self.assertEqual(data["conversation_id"], conv_id)

    def test_get_conversation(self) -> None:
        # Create a conversation
        response = self.client.post(
            "/chat",
            json={"message": "I need help"},
        )
        conv_id = response.json()["conversation_id"]

        # Retrieve it
        get_response = self.client.get(f"/conversations/{conv_id}")
        self.assertEqual(get_response.status_code, 200)
        data = get_response.json()
        self.assertEqual(data["conversation_id"], conv_id)
        self.assertEqual(len(data["messages"]), 2)  # user + agent

    def test_get_nonexistent_conversation(self) -> None:
        response = self.client.get("/conversations/nonexistent-id")
        self.assertEqual(response.status_code, 404)

    def test_list_conversations(self) -> None:
        # Create multiple conversations
        self.client.post("/chat", json={"message": "First issue"})
        self.client.post("/chat", json={"message": "Second issue"})

        response = self.client.get("/conversations")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(data["count"], 2)

    def test_delete_conversation(self) -> None:
        # Create a conversation
        response = self.client.post(
            "/chat",
            json={"message": "Test issue"},
        )
        conv_id = response.json()["conversation_id"]

        # Delete it
        delete_response = self.client.delete(f"/conversations/{conv_id}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["conversation_id"], conv_id)

        # Verify it's gone
        get_response = self.client.get(f"/conversations/{conv_id}")
        self.assertEqual(get_response.status_code, 404)

    def test_health_endpoint(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
