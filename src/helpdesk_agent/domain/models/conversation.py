from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4


class Conversation:
    """Store a multi-turn conversation with the helpdesk agent."""

    def __init__(self, conversation_id: str | None = None) -> None:
        self.conversation_id = conversation_id or str(uuid4().hex[:8])
        self.messages: list[dict[str, Any]] = []
        self.created_at = datetime.utcnow().isoformat()
        self.updated_at = datetime.utcnow().isoformat()
        self.context: dict[str, Any] = {}

    def add_user_message(self, content: str) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": content,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        self.updated_at = datetime.utcnow().isoformat()

    def add_agent_message(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        self.messages.append(
            {
                "role": "agent",
                "content": content,
                "metadata": metadata or {},
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        self.updated_at = datetime.utcnow().isoformat()

    def get_history(self) -> list[dict[str, Any]]:
        return self.messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "messages": self.messages,
            "context": self.context,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
