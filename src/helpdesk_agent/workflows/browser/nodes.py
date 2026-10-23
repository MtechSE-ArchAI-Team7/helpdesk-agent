from __future__ import annotations

import logging

from helpdesk_agent.domain.exceptions import LLMError
from helpdesk_agent.observability.workflow import _trace_step
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


@_trace_step("workflow.assess_playwright")
async def _assess_playwright_node(self: WorkflowContext, state: GraphState) -> GraphState:
    """Assess browser-test feasibility without invoking browser tools."""
    workflow_state = state["workflow_state"]
    case_id = workflow_state.case_id
    config = self.browser_settings
    skip_reason = None
    if not self.mcp_enabled:
        skip_reason = "mcp_disabled"
    elif not config.runtime_configured:
        skip_reason = "runtime_not_configured"
    elif not config.allowed_hosts:
        skip_reason = "host_allowlist_empty"

    if skip_reason:
        state["playwright_assessment"] = None
        workflow_state.record_history("playwright_assessment", "skipped", skip_reason)
        logger.info(
            "case=%s stage=playwright_assessment status=skipped reason=%s",
            case_id,
            skip_reason,
        )
        return state

    subject, body = self._browser_report(state)
    triage_result = state.get("triage_result")
    try:
        assessment = await self.llm_client.assess_playwright_validation(
            subject,
            body,
            triage_result.summary if triage_result else workflow_state.summary,
        )
    except LLMError as exc:
        state["playwright_assessment"] = None
        workflow_state.record_history(
            "playwright_assessment",
            "failed",
            "Feasibility assessment failed; continuing to FAQ",
        )
        logger.warning(
            "case=%s stage=playwright_assessment status=failed error_type=%s",
            case_id,
            type(exc).__name__,
        )
        return state

    report = f"{subject}\n\n{body}"
    if assessment.decision == "eligible":
        host, reason = self.playwright_flow.validate_target(assessment.target_url, report)
        if host is None:
            assessment = assessment.model_copy(
                update={"decision": "not_applicable", "reason": reason, "target_url": None}
            )
        elif not assessment.expected_error.strip():
            assessment = assessment.model_copy(
                update={
                    "decision": "needs_info",
                    "reason": "expected_error_not_specified",
                    "clarifying_questions": assessment.clarifying_questions
                    or ["What exact error message or unexpected behavior should the browser check look for?"],
                    "missing_info": assessment.missing_info or ["expected browser-visible error"],
                }
            )

    state["playwright_assessment"] = assessment
    if assessment.decision == "needs_info" and triage_result:
        questions = assessment.clarifying_questions or [
            "Please share the affected page URL and the steps that lead to the error. "
            "Do not include passwords or tokens."
        ]
        missing_info = assessment.missing_info or ["browser validation details"]
        merged_questions = list(dict.fromkeys([*triage_result.clarifying_questions, *questions]))
        merged_missing = list(dict.fromkeys([*triage_result.missing_info, *missing_info]))
        triage_result = triage_result.model_copy(
            update={"clarifying_questions": merged_questions, "missing_info": merged_missing}
        )
        state["triage_result"] = triage_result
        workflow_state.triage = {
            **(workflow_state.triage or {}),
            "missing_info": merged_missing,
            "clarifying_questions": merged_questions,
        }

    host, _ = self.playwright_flow.validate_target(assessment.target_url, report)
    workflow_state.record_history(
        "playwright_assessment",
        assessment.decision,
        assessment.reason,
    )
    logger.info(
        "case=%s stage=playwright_assessment status=%s host=%s",
        case_id,
        assessment.decision,
        host or "none",
    )
    return state


@_trace_step("workflow.playwright_validate")
async def _playwright_validate_node(self: WorkflowContext, state: GraphState) -> GraphState:
    result = await self.browser_graph.ainvoke(
        {
            "parent": state,
            "host": None,
            "target_url": "",
            "expected_error": "",
            "started_at": 0.0,
            "inspection_order": [],
            "observations": [],
            "run_result": {},
            "result_status": "",
            "result_summary": "",
        }
    )
    parent: GraphState = result["parent"]
    return parent


def _browser_report(self: WorkflowContext, state: GraphState) -> tuple[str, str]:
    subject, body = self._split_subject_body(state["initial_input"])
    if self.memory:
        # Only this ticket's USER turns may supply a previously reported URL.
        # Never authorize navigation from another case or an assistant suggestion.
        prior_reports = [
            snippet
            for snippet in state["workflow_state"].metadata.get("ticket_memory", [])
            if snippet.startswith("USER:")
        ]
        if prior_reports:
            body += "\n\nPrevious user reports on this same ticket (untrusted evidence):\n" + "\n".join(prior_reports)
    return subject, body
