from __future__ import annotations

import logging

from helpdesk_agent.domain.exceptions import MemoryError
from helpdesk_agent.domain.policies.tickets import extract_ticket_id
from helpdesk_agent.observability.workflow import _trace_step
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


@_trace_step("workflow.load_ticket")
async def _load_ticket_node(self: WorkflowContext, state: GraphState) -> GraphState:
    """Detect a ticket ID in the subject and load the existing Jira ticket."""
    workflow_state = state["workflow_state"]
    initial_input = state["initial_input"]
    subject, _ = self._split_subject_body(initial_input)

    ticket_id = extract_ticket_id(subject, self.project_key)
    if self.memory and initial_input.get("ticket_id"):
        # Chat/A2A bindings are server-side, never accepted directly from the HTTP request.
        ticket_id = initial_input["ticket_id"]
    if not ticket_id:
        logger.info("case=%s stage=load_ticket decision=new reason=no_ticket_id", workflow_state.case_id)
        state["ticket_id"] = None
        state["existing_ticket"] = None
        return state

    logger.info("case=%s stage=load_ticket ticket_id=%s", workflow_state.case_id, ticket_id)
    result = await self.ticket_flow.get_ticket(ticket_id)
    if result.get("status") in ("success", "mocked"):
        state["ticket_id"] = ticket_id
        state["existing_ticket"] = result
        workflow_state.case_id = ticket_id
        workflow_state.ticket = result
        workflow_state.record_history("load_ticket", "completed", f"Loaded existing ticket {ticket_id}")
        logger.info("case=%s stage=load_ticket status=found ticket_id=%s", ticket_id, ticket_id)
    else:
        if self.memory and initial_input.get("ticket_id"):
            logger.error("case=%s stage=load_ticket status=failed reason=linked_ticket_unavailable", ticket_id)
            raise MemoryError("The conversation's linked Jira ticket could not be loaded; refusing to create another")
        # Stale/unknown reference: treat as a new issue.
        state["ticket_id"] = None
        state["existing_ticket"] = None
        workflow_state.record_history(
            "load_ticket",
            "not_found",
            f"Ticket {ticket_id} not found in Jira; treating as new issue",
        )
        logger.warning(
            "case=%s stage=load_ticket status=not_found ticket_id=%s error=%s",
            workflow_state.case_id,
            ticket_id,
            result.get("error", "unknown"),
        )
    return state


@_trace_step("workflow.create_ticket")
async def _create_ticket_node(self: WorkflowContext, state: GraphState) -> GraphState:
    """Create a new Jira ticket for a new issue."""
    workflow_state = state["workflow_state"]
    initial_input = state["initial_input"]
    subject, body = self._split_subject_body(initial_input)

    logger.info("case=%s stage=create_ticket status=started", workflow_state.case_id)
    ticket_result = await self.ticket_flow.create_ticket(
        workflow_state.case_id,
        subject,
        body,
        "unknown",
        "medium",
        initial_input.get("sender", "unknown"),
    )
    workflow_state.ticket = ticket_result
    workflow_state.record_history(
        "create_ticket",
        ticket_result.get("status", "failed"),
        f"Ticket creation: {ticket_result.get('ticket_id', ticket_result.get('error', 'unknown'))}",
    )
    ticket_id = ticket_result.get("ticket_id")
    if ticket_result.get("status") == "failed" or not ticket_id:
        logger.error(
            "case=%s stage=create_ticket status=failed error=%s",
            workflow_state.case_id,
            ticket_result.get("error", "no ticket id in response"),
        )
        workflow_state.current_step = "ticket_creation_failed"
        return state
    state["ticket_id"] = ticket_id
    workflow_state.case_id = ticket_id
    if self.memory:
        await self._save_incoming_memory(state, ticket_id)
    logger.info(
        "case=%s stage=create_ticket status=%s ticket_id=%s",
        workflow_state.case_id,
        ticket_result.get("status", "failed"),
        ticket_id,
    )
    return state


@_trace_step("workflow.update_ticket")
async def _update_ticket_node(self: WorkflowContext, state: GraphState) -> GraphState:
    """Update the Jira ticket's fields to reflect the latest triage results."""
    workflow_state = state["workflow_state"]
    triage_result = state.get("triage_result")
    ticket_id = state.get("ticket_id")
    case_id = workflow_state.case_id

    if triage_result is None or not ticket_id:
        logger.info("case=%s stage=update_ticket note=skipped", case_id)
        return state

    update_result = await self.ticket_flow.update_ticket(
        ticket_id,
        summary=triage_result.summary,
        description=triage_result.raw_text,
        priority=triage_result.priority,
    )
    workflow_state.record_history(
        "update_ticket",
        update_result.get("status", "failed"),
        f"Ticket {ticket_id} fields updated: {update_result.get('updated', update_result.get('error', 'unknown'))}",
    )
    logger.info(
        "case=%s stage=update_ticket status=%s ticket_id=%s",
        case_id,
        update_result.get("status", "failed"),
        ticket_id,
    )
    return state
