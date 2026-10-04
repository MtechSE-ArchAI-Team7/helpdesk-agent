import os
import unittest
from unittest.mock import patch

from helpdesk_agent.config import LLMConfig, MCPConfig, TriageConfig


class TestTriageConfig(unittest.TestCase):
    def test_from_env_uses_default_clarification_round_limit(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = TriageConfig.from_env()
        self.assertEqual(config.max_clarification_rounds, 2)

    def test_from_env_reads_custom_clarification_round_limit(self) -> None:
        with patch.dict(os.environ, {"HELPDESK_AGENT_MAX_CLARIFICATION_ROUNDS": "4"}, clear=True):
            config = TriageConfig.from_env()
        self.assertEqual(config.max_clarification_rounds, 4)

    def test_rejects_negative_clarification_round_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be non-negative"):
            TriageConfig(max_clarification_rounds=-1)


class TestLLMConfig(unittest.TestCase):
    def test_from_env_normalizes_bedrock_gateway_base_url(self) -> None:
        environment = {
            "HELPDESK_AGENT_LLM_API_KEY": "test-key",
            "HELPDESK_AGENT_LLM_BASE_URL": "https://gateway123.gateway.bedrock-agentcore.us-west-2.amazonaws.com",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = LLMConfig.from_env()
            self.assertEqual(
                config.base_url,
                "https://gateway123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/inference/v1",
            )


class TestMCPConfig(unittest.TestCase):
    def test_from_env_uses_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = MCPConfig.from_env()
            self.assertEqual(config.base_url, "http://127.0.0.1:8765/mcp")
            self.assertEqual(config.timeout_seconds, 5.0)
            self.assertFalse(config.enable_async)
            self.assertIsNone(config.auth_token)
            self.assertEqual(config.headers, {})
            self.assertIsNone(config.faq_tool_prefix)
            self.assertEqual(config.faq_auth_mode, "bearer")
            self.assertIsNone(config.faq_region)
            self.assertIsNone(config.playwright_url)
            self.assertEqual(config.playwright_timeout_seconds, 30.0)
            self.assertEqual(config.playwright_allowed_hosts, [])
            self.assertEqual(config.playwright_tool_prefix, "mcp-playwright___")

    def test_from_env_reads_custom_values(self) -> None:
        environment = {
            "HELPDESK_AGENT_MCP_BASE_URL": "https://workiq.example.com/mcp",
            "HELPDESK_AGENT_MCP_TIMEOUT_SECONDS": "10.5",
            "HELPDESK_AGENT_MCP_ENABLE_ASYNC": "1",
            "HELPDESK_AGENT_MCP_AUTH_TOKEN": "secret-token",
            "HELPDESK_AGENT_MCP_FAQ_TOOL_PREFIX": "ops-knowledge___",
            "HELPDESK_AGENT_MCP_FAQ_AUTH_MODE": "IAM",
            "HELPDESK_AGENT_MCP_FAQ_REGION": "ap-southeast-2",
            "HELPDESK_AGENT_MCP_PLAYWRIGHT_URL": "https://playwright.example.com/mcp",
            "HELPDESK_AGENT_MCP_PLAYWRIGHT_TIMEOUT_SECONDS": "45",
            "HELPDESK_AGENT_PLAYWRIGHT_ALLOWED_HOSTS": "portal.example.com, internal.example.com.",
            "HELPDESK_AGENT_MCP_PLAYWRIGHT_TOOL_PREFIX": "mcp-atlassian___",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = MCPConfig.from_env()
            self.assertEqual(config.base_url, "https://workiq.example.com/mcp")
            self.assertEqual(config.timeout_seconds, 10.5)
            self.assertTrue(config.enable_async)
            self.assertEqual(config.auth_token, "secret-token")
            self.assertEqual(config.headers["Authorization"], "Bearer secret-token")
            self.assertEqual(config.faq_tool_prefix, "ops-knowledge___")
            self.assertEqual(config.faq_auth_mode, "iam")
            self.assertEqual(config.faq_region, "ap-southeast-2")
            self.assertEqual(config.playwright_url, "https://playwright.example.com/mcp")
            self.assertEqual(config.playwright_timeout_seconds, 45.0)
            self.assertEqual(config.playwright_allowed_hosts, ["portal.example.com", "internal.example.com"])
            self.assertEqual(config.playwright_tool_prefix, "mcp-atlassian___")


if __name__ == "__main__":
    unittest.main()
