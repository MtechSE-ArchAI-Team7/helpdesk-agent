from __future__ import annotations

import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from helpdesk_agent.domain.exceptions import LLMError
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


class BrowserState(TypedDict):
    parent: GraphState
    host: str | None
    target_url: str
    expected_error: str
    started_at: float
    run_result: dict[str, Any]
    result_status: str
    result_summary: str


def build_browser_graph(context: WorkflowContext) -> Any:
    def revalidate(state: BrowserState) -> BrowserState:
        parent = state["parent"]
        workflow_state = parent["workflow_state"]
        assessment = parent.get("playwright_assessment")
        subject, body = context._browser_report(parent)
        report = f"{subject}\n\n{body}"
        if assessment is None or assessment.decision != "eligible" or not assessment.target_url:
            workflow_state.record_history(
                "playwright_validation",
                "skipped",
                "No eligible browser validation target",
            )
            logger.warning(
                "case=%s stage=playwright_validation status=skipped reason=assessment_missing",
                workflow_state.case_id,
            )
            return state
        host, reason = context.playwright_flow.validate_target(assessment.target_url, report)
        if host is None:
            workflow_state.record_history("playwright_validation", "skipped", reason)
            logger.warning(
                "case=%s stage=playwright_validation status=skipped reason=%s",
                workflow_state.case_id,
                reason,
            )
            return state
        state["host"] = host
        state["target_url"] = assessment.target_url
        state["expected_error"] = assessment.expected_error
        state["started_at"] = time.monotonic()
        logger.info(
            "case=%s stage=playwright_validation status=started host=%s",
            workflow_state.case_id,
            host,
        )
        return state

    async def execute(state: BrowserState) -> BrowserState:
        state["run_result"] = await context.playwright_flow.validate(state["target_url"])
        for tool_status in state["run_result"].get("tool_statuses", []):
            logger.info(
                "case=%s stage=playwright_validation tool=%s status=%s",
                state["parent"]["workflow_state"].case_id,
                tool_status.get("tool", "unknown"),
                tool_status.get("status", "unknown"),
            )
        return state

    async def evaluate(state: BrowserState) -> BrowserState:
        result = state["run_result"]
        if result.get("status") == "completed":
            try:
                evaluation = await context.llm_client.evaluate_playwright_validation(
                    state["expected_error"],
                    result.get("evidence", ""),
                )
                state["result_status"] = evaluation.status
                state["result_summary"] = evaluation.summary
            except LLMError:
                state["result_status"] = "inconclusive"
                state["result_summary"] = "Browser evidence was collected but could not be evaluated."
        else:
            state["result_status"] = "inconclusive"
            state["result_summary"] = "Browser validation did not complete; no conclusion was reached."
        return state

    async def record(state: BrowserState) -> BrowserState:
        parent = state["parent"]
        workflow_state = parent["workflow_state"]
        run_result = state["run_result"]
        result_status, result_summary, host = state["result_status"], state["result_summary"], state["host"]
        duration_ms = int((time.monotonic() - state["started_at"]) * 1000)
        validation = {
            "status": result_status,
            "summary": result_summary,
            "host": host,
            "tool_statuses": run_result.get("tool_statuses", []),
        }
        parent["playwright_validation"] = validation
        workflow_state.metadata["playwright_validation"] = validation
        workflow_state.evidence.append(
            {"source": "playwright", "status": result_status, "summary": result_summary, "host": host},
        )
        workflow_status = "completed" if run_result.get("status") == "completed" else "failed"
        workflow_state.record_history(
            "playwright_validation",
            workflow_status,
            f"outcome={result_status}; host={host}; duration_ms={duration_ms}",
        )
        log_method = logger.info if workflow_status == "completed" else logger.warning
        log_method(
            "case=%s stage=playwright_validation status=%s outcome=%s host=%s duration_ms=%d",
            workflow_state.case_id,
            workflow_status,
            result_status,
            host,
            duration_ms,
        )
        ticket_id = parent.get("ticket_id")
        if ticket_id:
            await context.ticket_flow.add_comment(
                ticket_id,
                f"Playwright validation: {result_status}. {result_summary} (host: {host})",
            )
        return state

    def next_step(state: BrowserState) -> str:
        return "execute" if state["host"] is not None else END

    graph = StateGraph(BrowserState)
    graph.add_node("revalidate", revalidate)
    graph.add_node("execute", execute)
    graph.add_node("evaluate", evaluate)
    graph.add_node("record", record)
    graph.set_entry_point("revalidate")
    graph.add_conditional_edges("revalidate", next_step, {"execute": "execute", END: END})
    graph.add_edge("execute", "evaluate")
    graph.add_edge("evaluate", "record")
    graph.add_edge("record", END)
    return graph.compile()
