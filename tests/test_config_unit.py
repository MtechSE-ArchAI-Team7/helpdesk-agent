import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpdesk_agent.config import A2AConfig, LangfuseConfig, LLMConfig, MemoryConfig, TriageConfig
from helpdesk_agent.modules.playwright import PlaywrightAssessment, PlaywrightEvaluation
from helpdesk_agent.modules.triage.llm import LLMError, LLMTriageClient
from helpdesk_agent.modules.triage.schema import TriageResult

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("raw", ["invalid", "[]", '{"agent": 123}'])
def test_remote_agent_map_rejects_invalid_json_or_shape(monkeypatch, raw):
    monkeypatch.setenv("HELPDESK_AGENT_A2A_REMOTE_AGENTS", raw)
    with pytest.raises(ValueError, match="JSON object"):
        A2AConfig.from_env()


@pytest.mark.parametrize("secret", [" plain-token ", '{"token":"resolved-token"}'])
def test_remote_agent_secret_resolution_preserves_explicit_overrides(monkeypatch, secret):
    monkeypatch.setenv("HELPDESK_AGENT_A2A_REMOTE_AGENTS", '{"diagnosis-agent":"https://example.com"}')
    monkeypatch.setenv("HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS", '{"local":"explicit-token"}')
    monkeypatch.setenv(
        "HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKEN_SECRET_ARNS",
        '{"local":"local-secret","diagnosis-agent":"remote-secret"}',
    )
    monkeypatch.setenv("HELPDESK_AGENT_A2A_REMOTE_AGENT_ARNS", '{"diagnosis-agent":"test-runtime"}')
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": secret}
    with patch("boto3.client", return_value=client):
        config = A2AConfig.from_env()
    assert config.remote_agent_tokens["local"] == "explicit-token"
    assert config.remote_agent_tokens["diagnosis-agent"] == ("plain-token" if "{" not in secret else "resolved-token")
    assert config.remote_agent_runtime_arns == {"diagnosis-agent": "test-runtime"}
    client.get_secret_value.assert_called_once_with(SecretId="remote-secret")


@pytest.mark.parametrize("value", ['{"first":"one","second":"two"}', "[]"])
def test_secret_token_resolution_rejects_ambiguous_json(value):
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": value}
    with patch("boto3.client", return_value=client):
        with pytest.raises(ValueError, match="expected a plain string"):
            A2AConfig._resolve_secret_tokens({"agent": "secret-name"})


def test_memory_and_triage_environment_validation(monkeypatch):
    monkeypatch.setenv("HELPDESK_AGENT_MEMORY_ENABLED", "true")
    with pytest.raises(ValueError, match="0 or 1"):
        MemoryConfig.from_env()
    with pytest.raises(ValueError, match="non-negative"):
        TriageConfig(max_clarification_rounds=-1)


@pytest.mark.parametrize("secret", ["[]", '{"public_key":"test"}'])
def test_langfuse_secret_requires_complete_credentials(monkeypatch, secret):
    monkeypatch.setenv("HELPDESK_AGENT_LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("HELPDESK_AGENT_LANGFUSE_SECRET_ARN", "test-secret")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": secret}
    with patch("boto3.client", return_value=client):
        with pytest.raises(ValueError, match="JSON object|requires public and secret"):
            LangfuseConfig.from_env()


@pytest.mark.parametrize(
    "url,expected",
    [
        (None, None),
        ("https://bedrock-agentcore.example/", "https://bedrock-agentcore.example/inference/v1"),
        ("https://example.com/inference", "https://example.com/inference/v1"),
        ("https://example.com/inference/v1", "https://example.com/inference/v1"),
        ("https://example.com/v1/", "https://example.com/v1"),
    ],
)
def test_llm_gateway_url_normalization(url, expected):
    assert LLMConfig._normalize_base_url(url) == expected


def test_llm_temperature_default_and_environment(monkeypatch):
    monkeypatch.delenv("HELPDESK_AGENT_LLM_TEMPERATURE", raising=False)
    assert LLMConfig.from_env().temperature == 0.1
    monkeypatch.setenv("HELPDESK_AGENT_LLM_TEMPERATURE", "1")
    assert LLMConfig.from_env().temperature == 1.0


@pytest.mark.parametrize("temperature", [-0.01, 2.01, float("nan"), float("inf")])
def test_llm_temperature_rejects_values_outside_supported_range(temperature):
    with pytest.raises(ValueError, match="HELPDESK_AGENT_LLM_TEMPERATURE"):
        LLMConfig(api_key="test", temperature=temperature)


@pytest.mark.parametrize(
    "method,args",
    [
        ("triage_email", ("Subject", "Body")),
        ("assess_playwright_validation", ("Subject", "Body", "Summary")),
        ("evaluate_playwright_validation", ("Error", "Evidence")),
    ],
)
@pytest.mark.parametrize("content", ["", "[]", "invalid", '{"confidence":2}'])
@pytest.mark.asyncio
async def test_llm_response_failures_are_explicit(method, args, content):
    guardrails = MagicMock()
    guardrails.validate_input = AsyncMock()
    guardrails.validate_output = AsyncMock()
    with patch("helpdesk_agent.modules.triage.llm.AsyncOpenAI") as constructor:
        response = MagicMock()
        response.choices[0].message.content = content
        constructor.return_value.chat.completions.create = AsyncMock(return_value=response)
        client = LLMTriageClient(LLMConfig(api_key="test"), guardrails=guardrails)
        with pytest.raises(LLMError):
            await getattr(client, method)(*args)


@pytest.mark.asyncio
async def test_llm_request_uses_configured_temperature():
    guardrails = MagicMock()
    guardrails.validate_input = AsyncMock()
    guardrails.validate_output = AsyncMock()
    with patch("helpdesk_agent.modules.triage.llm.AsyncOpenAI") as constructor:
        response = MagicMock()
        response.choices[0].message.content = json.dumps(
            {
                "service": "identity",
                "issue_type": "access",
                "priority": "medium",
                "confidence": 0.9,
                "summary": "Account access issue",
            }
        )
        constructor.return_value.chat.completions.create = AsyncMock(return_value=response)
        constructor.return_value.close = AsyncMock()
        client = LLMTriageClient(LLMConfig(api_key="test", temperature=1.0), guardrails=guardrails)
        await client.triage_email("Subject", "Body")
        assert client.client.chat.completions.create.await_args.kwargs["temperature"] == 1.0
        await client.client.close()


@pytest.mark.asyncio
async def test_llm_drafts_and_missing_key():
    guardrails = MagicMock()
    with pytest.raises(LLMError, match="API_KEY"):
        LLMTriageClient(LLMConfig(), guardrails=guardrails)
    with patch("helpdesk_agent.modules.triage.llm.AsyncOpenAI"):
        client = LLMTriageClient(LLMConfig(api_key="test"), guardrails=guardrails)
        result = TriageResult(
            service="identity",
            issue_type="access",
            priority="high",
            confidence=0.5,
            summary="Login",
            source="email",
            sender="u@example.com",
            raw_text="Login",
        )
        assert "more details" in await client.draft_clarification(result, "Login")
        result.clarifying_questions = ["Which browser?", "What error?"]
        drafted = await client.draft_clarification(result, "Login")
        assert "- Which browser?" in drafted and "- What error?" in drafted
        assert "**Reset**" in await client.draft_resolution({"title": "Reset", "body": "Steps"}, "Login")
        assert "**Solution**" in await client.draft_resolution({}, "Login")


@pytest.mark.parametrize(
    "method,model,payload,args",
    [
        (
            "assess_playwright_validation",
            PlaywrightAssessment,
            {"decision": "not_applicable", "reason": "Not a browser issue"},
            ("Subject", "Body", "Summary"),
        ),
        (
            "evaluate_playwright_validation",
            PlaywrightEvaluation,
            {"status": "not_reproduced", "summary": "Page works"},
            ("Error", "Evidence"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_llm_browser_models_are_validated(method, model, payload, args):
    guardrails = MagicMock()
    guardrails.validate_input = AsyncMock()
    guardrails.validate_output = AsyncMock()
    with patch("helpdesk_agent.modules.triage.llm.AsyncOpenAI") as constructor:
        response = MagicMock()
        response.choices[0].message.content = json.dumps(payload)
        constructor.return_value.chat.completions.create = AsyncMock(return_value=response)
        client = LLMTriageClient(LLMConfig(api_key="test"), guardrails=guardrails)
        result = await getattr(client, method)(*args)
        assert isinstance(result, model)
        guardrails.validate_output.assert_awaited_once_with(json.dumps(payload), model)
