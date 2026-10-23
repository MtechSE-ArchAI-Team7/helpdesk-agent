from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from helpdesk_agent.config import LLMConfig
from helpdesk_agent.domain.exceptions import LLMError
from helpdesk_agent.observability import generation, record_generation_response
from helpdesk_agent.ports.capabilities import ChatMessage


class OpenAITransport:
    def __init__(self, config: LLMConfig) -> None:
        if not config.api_key:
            raise LLMError(
                "HELPDESK_AGENT_LLM_API_KEY environment variable is required. "
                "Set it to your Bedrock gateway auth token or OpenAI API key."
            )
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )

    @asynccontextmanager
    async def generate(
        self, name: str, messages: list[ChatMessage], *, task: str = "triage"
    ) -> AsyncIterator[str | None]:
        provider_messages: list[ChatCompletionMessageParam] = []
        for message in messages:
            if message["role"] == "system":
                provider_messages.append({"role": "system", "content": message["content"]})
            else:
                provider_messages.append({"role": "user", "content": message["content"]})
        model = self.config.model_for(task)
        with generation(name, model, messages) as span:
            response = await self.client.chat.completions.create(
                model=model,
                messages=provider_messages,
                response_format={"type": "json_object"},
                temperature=self.config.temperature,
            )
            record_generation_response(span, response)
            yield response.choices[0].message.content
