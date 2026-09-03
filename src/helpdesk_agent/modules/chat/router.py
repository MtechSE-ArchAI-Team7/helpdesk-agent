from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.workflow import HelpdeskWorkflow
from ...dependencies import get_conversation_store, get_workflow
from .schema import ChatMessage, ChatResponse
from .service import ConversationStore, handle_chat_message

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatMessage,
    workflow: Annotated[HelpdeskWorkflow, Depends(get_workflow)],
    conversation_store: Annotated[ConversationStore, Depends(get_conversation_store)],
) -> ChatResponse:
    """Chat with the helpdesk agent.

    Start a new conversation by omitting conversation_id, or continue an existing one.
    """
    return await handle_chat_message(workflow, conversation_store, request.message, request.conversation_id)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    conversation_store: Annotated[ConversationStore, Depends(get_conversation_store)],
) -> dict[str, Any]:
    """Retrieve a conversation by ID."""
    conv = conversation_store.get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv.to_dict()


@router.get("/conversations")
async def list_conversations(
    conversation_store: Annotated[ConversationStore, Depends(get_conversation_store)],
) -> dict[str, Any]:
    """List all conversations."""
    convs = conversation_store.list_all()
    return {
        "count": len(convs),
        "conversations": [c.to_dict() for c in convs],
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    conversation_store: Annotated[ConversationStore, Depends(get_conversation_store)],
) -> dict[str, Any]:
    """Delete a conversation."""
    if conversation_store.delete(conversation_id):
        return {"status": "deleted", "conversation_id": conversation_id}
    raise HTTPException(status_code=404, detail="Conversation not found")
