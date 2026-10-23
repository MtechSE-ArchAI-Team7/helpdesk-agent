from __future__ import annotations

from typing import Any, Protocol

from helpdesk_agent.domain.models.conversation import Conversation
from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.ports.capabilities import MemoryPort


class WorkflowRunner(Protocol):
    memory: MemoryPort | None

    async def run(self, initial_state: dict[str, Any]) -> WorkflowState: ...

    async def save_chat_response(self, state: WorkflowState, content: str, identity: str) -> None: ...


class ConversationRepository(Protocol):
    def get_or_create(self, conversation_id: str | None = None) -> Conversation: ...
