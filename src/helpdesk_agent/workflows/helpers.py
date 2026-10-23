from __future__ import annotations

import logging
from typing import Any

from helpdesk_agent.application import memory as memory_service
from helpdesk_agent.application.communication import _ticketed_subject as ticketed_subject
from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


def _split_subject_body(initial_input: dict[str, Any]) -> tuple[str, str]:
    """Split the incoming input into (subject, body)."""
    subject = initial_input.get("subject", "")
    summary_text = initial_input.get("summary", "No summary provided")
    parts = summary_text.split("\n\n", 1)
    if not subject:
        subject = parts[0] if parts else "No subject"
    body = parts[1] if len(parts) > 1 else summary_text
    return subject, body


def _ticketed_subject(self: WorkflowContext, ticket_id: str | None, subject: str) -> str:
    return ticketed_subject(ticket_id, subject, self.project_key)


async def save_chat_response(self: WorkflowContext, state: WorkflowState, content: str, identity: str) -> None:
    await memory_service.save_chat_response(self.memory, state, content, identity)


async def _save_incoming_memory(self: WorkflowContext, graph_state: GraphState, ticket_id: str) -> None:
    await memory_service._save_incoming_memory(
        self.memory, graph_state["workflow_state"], graph_state["initial_input"], ticket_id
    )


async def _save_ticket_memory(self: WorkflowContext, graph_state: GraphState) -> None:
    await memory_service._save_ticket_memory(
        self.memory,
        graph_state["workflow_state"],
        graph_state["initial_input"],
        graph_state.get("existing_ticket") or {},
    )
