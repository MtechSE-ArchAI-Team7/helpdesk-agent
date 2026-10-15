import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


class TestEmailReadChat(unittest.TestCase):
    def setUp(self) -> None:
        # Mock the LLM client to avoid requiring API key
        self.mock_patcher = patch("helpdesk_agent.dependencies.get_llm_client")
        mock_get_llm = self.mock_patcher.start()

        from helpdesk_agent.modules.triage.schema import TriageResult

        mock_llm = MagicMock()
        mock_llm.triage_email = AsyncMock(
            return_value=TriageResult(
                service="email",
                issue_type="general",
                priority="low",
                confidence=0.9,
                missing_info=[],
                clarifying_questions=[],
                summary="Email read request",
                source="chat",
                sender="test@example.com",
                raw_text="Can you read my emails?",
            )
        )
        mock_get_llm.return_value = mock_llm

        from helpdesk_agent.main import create_app

        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.mock_patcher.stop()

    def test_read_email_request(self) -> None:
        response = self.client.post(
            "/chat",
            json={"message": "Can you read my emails?"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("agent_response", data)
        # Should detect email read request
        self.assertIn("unread", data["agent_response"].lower())

    def test_fetch_email_request(self) -> None:
        response = self.client.post(
            "/chat",
            json={"message": "Fetch my latest emails"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("agent_response", data)
        self.assertIn("email", data["agent_response"].lower())

    def test_get_messages_request(self) -> None:
        response = self.client.post(
            "/chat",
            json={"message": "Get all my messages"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("agent_response", data)

    def test_normal_request_still_creates_ticket(self) -> None:
        response = self.client.post(
            "/chat",
            json={"message": "I need help with my account"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("case_id", data)
        # Should create ticket, not fetch emails
        self.assertNotIn("unread", data["agent_response"].lower())


if __name__ == "__main__":
    unittest.main()
