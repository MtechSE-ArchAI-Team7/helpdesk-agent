from __future__ import annotations

from typing import Any, TypedDict

from helpdesk_agent.domain.models.browser import PlaywrightAssessment
from helpdesk_agent.domain.models.triage import TriageResult
from helpdesk_agent.domain.models.workflow import WorkflowState


class GraphState(TypedDict):
    """State for the LangGraph workflow."""

    workflow_state: WorkflowState
    initial_input: dict[str, Any]
    triage_result: TriageResult | None
    faq_results: dict[str, Any] | None
    ticket_id: str | None
    existing_ticket: dict[str, Any] | None
    playwright_assessment: PlaywrightAssessment | None
    playwright_validation: dict[str, Any] | None
