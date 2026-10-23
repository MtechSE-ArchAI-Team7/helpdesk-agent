from __future__ import annotations

from helpdesk_agent.domain.models.conversation import Conversation


class ConversationStore:
    """Simple in-memory store for conversations."""

    def __init__(self) -> None:
        self.conversations: dict[str, Conversation] = {}

    def create(self, conversation_id: str | None = None) -> Conversation:
        conv = Conversation(conversation_id)
        self.conversations[conv.conversation_id] = conv
        return conv

    def get(self, conversation_id: str) -> Conversation | None:
        return self.conversations.get(conversation_id)

    def get_or_create(self, conversation_id: str | None = None) -> Conversation:
        if conversation_id and conversation_id in self.conversations:
            return self.conversations[conversation_id]
        return self.create(conversation_id)

    def delete(self, conversation_id: str) -> bool:
        if conversation_id in self.conversations:
            del self.conversations[conversation_id]
            return True
        return False

    def list_all(self) -> list[Conversation]:
        return list(self.conversations.values())
