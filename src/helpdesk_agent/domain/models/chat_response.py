from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    message: str = Field(..., description="The user's message")
    conversation_id: str | None = Field(None, description="Existing conversation ID")


class ChatResponse(BaseModel):
    conversation_id: str
    agent_response: str
    case_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
