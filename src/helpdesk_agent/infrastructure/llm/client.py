from __future__ import annotations

from helpdesk_agent.config import LLMConfig
from helpdesk_agent.domain.exceptions import LLMError as LLMError
from helpdesk_agent.infrastructure.llm.transport import OpenAITransport
from helpdesk_agent.reasoning.client import HelpdeskReasoner
from helpdesk_agent.reasoning.guardrails import GuardrailsValidator, get_guardrails_validator


class LLMTriageClient(HelpdeskReasoner):
    """Compatibility client combining the existing provider and reasoning defaults."""

    def __init__(self, config: LLMConfig | None = None, guardrails: GuardrailsValidator | None = None) -> None:
        self.config = config or LLMConfig.from_env()
        transport = OpenAITransport(self.config)
        self.client = transport.client
        super().__init__(transport, guardrails or get_guardrails_validator())
