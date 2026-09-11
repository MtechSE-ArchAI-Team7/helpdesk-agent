from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .schema import ChatResponse

if TYPE_CHECKING:
    from ...core.workflow import HelpdeskWorkflow


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


async def handle_chat_message(
    workflow: HelpdeskWorkflow,
    conversation_store: ConversationStore,
    message: str,
    conversation_id: str | None = None,
    *,
    source: str = "chat",
    sender: str = "chat-user",
) -> ChatResponse:
    """Run one user message through the workflow inside a conversation.

    Shared by the /chat HTTP endpoint and the A2A executor. When
    ``conversation_id`` is provided the existing conversation is continued,
    which is how multi-turn clarification flows resume.
    """
    conv = conversation_store.get_or_create(conversation_id)
    conv.add_user_message(message)

    # Process the user message through the workflow
    case_id = f"chat-{conv.conversation_id}"
    state = await workflow.run(
        {
            "case_id": case_id,
            "summary": message,
            "source": source,
            "sender": sender,
            "metadata": {
                "conversation_id": conv.conversation_id,
            },
        }
    )

    # Format agent response
    if state.current_step == "email_retrieved":
        email_messages = state.metadata.get("email_messages", [])
        if email_messages:
            agent_response = f"Found {len(email_messages)} unread email(s):\n"
            for i, msg in enumerate(email_messages, 1):
                agent_response += f"\n{i}. {msg.get('subject', 'No Subject')} from {msg.get('from', 'Unknown')}"
        else:
            agent_response = "No unread emails found."
    elif state.current_step == "awaiting_clarification":
        clarification = state.clarification or {}
        questions = clarification.get("questions", [])
        missing_info = clarification.get("missing_info", [])
        if questions:
            agent_response = "I need some more information to help with your request:\n"
            agent_response += "\n".join(f"- {q}" for q in questions)
        else:
            agent_response = "I need more details about your request. Could you describe the issue in more detail?"
        if missing_info:
            agent_response += f"\n\nMissing information: {', '.join(missing_info)}"
    elif state.current_step == "faq_answer_provided":
        faq_matches = state.faq_matches or []
        if faq_matches:
            top = faq_matches[0]
            agent_response = (
                f"I found a solution that may help:\n\n**{top.get('title', 'FAQ')}**\n\n{top.get('body', '')}"
            )
        else:
            agent_response = "Your request was resolved via our knowledge base."
    else:
        agent_response = f"Case {state.case_id}. Current step: {state.current_step}. "
        if state.ticket:
            agent_response += f"Ticket: {state.ticket.get('ticket_id', 'N/A')}. "
        if state.current_step == "diagnosis_required":
            agent_response += "Your ticket has been escalated for diagnosis by the support team."

    conv.add_agent_message(agent_response, {"case_id": state.case_id, "state": state.to_dict()})

    return ChatResponse(
        conversation_id=conv.conversation_id,
        agent_response=agent_response,
        case_id=state.case_id,
        metadata={
            "current_step": state.current_step,
            "ticket_id": state.ticket.get("ticket_id") if state.ticket else None,
            "clarifying_questions": (state.clarification or {}).get("questions", []),
        },
    )
