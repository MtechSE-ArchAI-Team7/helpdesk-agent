from __future__ import annotations

import logging

from helpdesk_agent.application.diagnosis import _trigger_diagnosis_agent as trigger_diagnosis
from helpdesk_agent.observability.workflow import _trace_step
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


@_trace_step("workflow.diagnose")
async def _diagnose_node(self: WorkflowContext, state: GraphState) -> GraphState:
    """Mark the ticket as requiring diagnosis (no FAQ match or clarification exhausted)."""
    workflow_state = state["workflow_state"]
    triage_result = state.get("triage_result")
    ticket_id = state.get("ticket_id")
    case_id = workflow_state.case_id

    logger.info("case=%s stage=diagnose status=started", case_id)

    rounds = self._clarification_rounds(state)
    triage_summary = triage_result.summary if triage_result else workflow_state.summary

    max_rounds = self.triage_config.max_clarification_rounds
    if rounds >= max_rounds:
        comment_body = (
            f"Diagnosis required — clarification attempts exhausted "
            f"({rounds}/{max_rounds} rounds).\n\n"
            f"Latest triage summary: {triage_summary}"
        )
    else:
        comment_body = f"Diagnosis required — no matching FAQ answer found.\n\nTriage summary: {triage_summary}"

    if ticket_id:
        comment_result = await self.ticket_flow.add_comment(ticket_id, comment_body)
        workflow_state.record_history(
            "diagnose",
            comment_result.get("status", "failed"),
            "Ticket comment added: diagnosis required",
        )
        logger.info(
            "case=%s stage=diagnose comment_status=%s ticket_id=%s",
            case_id,
            comment_result.get("status", "failed"),
            ticket_id,
        )
    else:
        workflow_state.record_history("diagnose", "skipped", "No ticket available for comment")

    await self._trigger_diagnosis_agent(state)

    workflow_state.current_step = "diagnosis_required"
    logger.info("case=%s stage=diagnose status=completed", case_id)
    return state


async def _trigger_diagnosis_agent(self: WorkflowContext, state: GraphState) -> None:
    await trigger_diagnosis(
        self.a2a_client_manager,
        self.ticket_flow,
        state["workflow_state"],
        state.get("triage_result"),
        state.get("ticket_id"),
    )
