from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from helpdesk_agent.domain.models.chat_response import ChatResponse
from helpdesk_agent.ports.execution import ConversationRepository

if TYPE_CHECKING:
    from helpdesk_agent.ports.execution import WorkflowRunner


async def handle_chat_message(
    workflow: WorkflowRunner,
    conversation_store: ConversationRepository,
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
    identity = uuid4().hex
    linked_ticket: str | None = None
    if workflow.memory:
        # Resolve before running: a failed read must not create a duplicate Jira ticket.
        linked_ticket = await workflow.memory.conversation_ticket(conv.conversation_id)
    conv.add_user_message(message)

    # Process the user message through the workflow
    case_id = f"chat-{conv.conversation_id}"
    state = await workflow.run(
        {
            "case_id": case_id,
            "summary": message,
            "source": source,
            "sender": sender,
            "ticket_id": linked_ticket,
            "memory_identity": identity,
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

    await workflow.save_chat_response(state, agent_response, identity)
    conv.add_agent_message(agent_response, {"case_id": state.case_id, "state": state.to_dict()})

    return ChatResponse(
        conversation_id=conv.conversation_id,
        agent_response=agent_response,
        case_id=state.case_id,
        metadata={
            "current_step": state.current_step,
            "ticket_id": state.ticket.get("ticket_id") if state.ticket else None,
            "clarifying_questions": (state.clarification or {}).get("questions", []),
            **(
                {
                    "similar_cases": state.metadata.get("similar_cases", []),
                    "memory_errors": state.metadata.get("memory_errors", []),
                }
                if workflow.memory
                else {}
            ),
        },
    )
