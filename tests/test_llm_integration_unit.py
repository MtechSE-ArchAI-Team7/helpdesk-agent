from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpdesk_agent.config.settings import LLMConfig
from tests.integration import test_llm

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("url", [None, "", "ftp://example.com", "https://user:password@example.com"])
def test_live_llm_rejects_missing_or_invalid_url(url):
    with pytest.raises(ValueError, match="BASE_URL"):
        test_llm._validate_config(LLMConfig(base_url=url))


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_live_llm_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="TIMEOUT_SECONDS"):
        test_llm._validate_config(LLMConfig(base_url="https://example.com/v1", timeout_seconds=timeout))


@pytest.mark.asyncio
async def test_external_llm_uses_config_without_retries(monkeypatch):
    result = SimpleNamespace(
        summary="Account locked",
        confidence=0.9,
        service="identity",
        issue_type="access",
        priority="medium",
    )
    client = SimpleNamespace(client=AsyncMock(), triage_email=AsyncMock(return_value=result))
    configs = []

    def factory(config):
        configs.append(config)
        return client

    monkeypatch.setattr(test_llm, "LLMTriageClient", factory)
    config = LLMConfig(base_url="https://example.com/v1")
    await test_llm.test_external_llm(config)
    assert configs == [config]
    assert client.client.max_retries == 0
    client.triage_email.assert_awaited_once()
    client.client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_triage_closes_client_on_failure(monkeypatch):
    client = SimpleNamespace(client=AsyncMock(), triage_email=AsyncMock(side_effect=RuntimeError("test failure")))
    monkeypatch.setattr(test_llm, "LLMTriageClient", lambda config: client)
    with pytest.raises(RuntimeError, match="test failure"):
        await test_llm.test_external_llm(LLMConfig(base_url="https://example.com/v1"))
    assert client.client.max_retries == 0
    client.client.close.assert_awaited_once()
