import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


class TestMainApp(unittest.TestCase):
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
                summary="Test summary",
                source="email",
                sender="test@example.com",
                raw_text="Test raw text",
            )
        )
        mock_get_llm.return_value = mock_llm

        from helpdesk_agent.main import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.mock_patcher.stop()

    def test_hello_world(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "helpdesk agent is running"})

    def test_healthz(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})
