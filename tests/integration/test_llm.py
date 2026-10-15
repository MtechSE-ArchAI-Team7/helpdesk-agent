from __future__ import annotations

import math
from urllib.parse import urlsplit

import pytest

from helpdesk_agent.config import LLMConfig
from helpdesk_agent.modules.triage.llm import LLMTriageClient

pytestmark = [pytest.mark.integration, pytest.mark.live_llm, pytest.mark.asyncio]


def _validate_config(config: LLMConfig) -> None:
    if not config.base_url:
        raise ValueError("Set HELPDESK_AGENT_LLM_BASE_URL; live tests never default to the public OpenAI endpoint")
    parsed = urlsplit(config.base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HELPDESK_AGENT_LLM_BASE_URL must be an HTTP(S) URL without embedded credentials")
    if not config.model.strip():
        raise ValueError("HELPDESK_AGENT_LLM_MODEL must not be empty")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ValueError("HELPDESK_AGENT_LLM_TIMEOUT_SECONDS must be finite and greater than zero")


async def test_external_llm(live_llm_config: LLMConfig) -> None:
    _validate_config(live_llm_config)
    client = LLMTriageClient(live_llm_config)
    try:
        client.client.max_retries = 0
        result = await client.triage_email(
            subject="Unable to sign in to the employee portal",
            body=(
                "Since 09:00 UTC today my employee portal login shows 'Account locked'. "
                "Only my account is affected. I use Chrome on Windows. "
                "Please help restore access; there is no organization-wide outage."
            ),
        )
    finally:
        await client.client.close()
    assert result.summary.strip(), "Triage summary is empty"
    assert 0 <= result.confidence <= 1
    assert result.service in {"jira", "slack", "email", "identity", "platform", "prometheus", "unknown"}
    assert result.issue_type in {"access", "incident", "performance", "deployment", "general"}
    assert result.priority in {"critical", "high", "medium", "low"}
