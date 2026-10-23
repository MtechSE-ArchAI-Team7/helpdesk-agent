from __future__ import annotations

import logging
from typing import Any

from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.observability.tracing import emit_workflow_event
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


def _record_gate_decision(
    state: GraphState,
    *,
    stage: str,
    decision: str,
    reason_code: str,
    details: dict[str, Any] | None = None,
) -> None:
    workflow_state = state["workflow_state"]
    if not isinstance(workflow_state, WorkflowState):
        raise TypeError("Workflow gate decisions require a WorkflowState")
    emit_workflow_event(
        workflow_state,
        event_type="gate.decision",
        stage=stage,
        actor_type="DETERMINISTIC",
        status="DECIDED",
        summary=f"{stage.replace('_', ' ').title()} selected {decision.replace('_', ' ')}",
        details={"decision": decision, "reason_code": reason_code, **(details or {})},
    )
