from __future__ import annotations

import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from helpdesk_agent.domain.exceptions import LLMError
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)
MAX_REACT_ROUNDS = 3


class BrowserState(TypedDict):
    parent: GraphState
    host: str | None
    target_url: str
    expected_error: str
    started_at: float
    inspection_order: list[str]
    observations: list[dict[str, Any]]
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

    async def plan(state: BrowserState) -> BrowserState:
        parent = state["parent"]
        workflow_state = parent["workflow_state"]
        try:
            browser_plan = await context.llm_client.plan_browser(
                state["target_url"],
                state["expected_error"],
                workflow_state.summary,
            )
        except LLMError as exc:
            logger.warning(
                "case=%s stage=playwright_planning status=failed error_type=%s",
                workflow_state.case_id,
                type(exc).__name__,
            )
            workflow_state.record_history(
                "playwright_planning",
                "failed",
                f"Browser plan unavailable ({type(exc).__name__})",
            )
            state["run_result"] = {
                "status": "planning_failed",
                "tool_statuses": [],
                "evidence": "",
                "error_type": type(exc).__name__,
            }
            state["result_status"] = "inconclusive"
            state["result_summary"] = "Browser planning failed; no browser tools were run."
            return state
        state["inspection_order"] = list(browser_plan.inspection_order)
        workflow_state.record_history(
            "playwright_planning",
            "completed",
            f"Created read-only browser plan with {len(browser_plan.inspection_order)} inspection steps",
        )
        return state

    async def react(state: BrowserState) -> BrowserState:
        parent = state["parent"]
        workflow_state = parent["workflow_state"]
        observations: list[dict[str, Any]] = []
        tool_statuses: list[dict[str, str]] = []
        try:
            async with context.playwright_flow.open_session(state["target_url"]) as browser:
                navigation = await browser.navigate()
                observations.append(navigation)
                tool_statuses.append({"tool": "browser_navigate", "status": str(navigation["status"])})
                if navigation["status"] != "completed":
                    raise RuntimeError("browser navigation failed")
                used_tools: set[str] = set()
                for round_number in range(1, MAX_REACT_ROUNDS + 1):
                    action = await context.llm_client.react_browser(
                        state["target_url"],
                        state["expected_error"],
                        workflow_state.summary,
                        state["inspection_order"],
                        observations,
                        round_number,
                    )
                    if action.action == "finish":
                        break
                    tool = action.tool
                    if tool is None or tool not in state["inspection_order"] or tool in used_tools:
                        raise ValueError("ReAct selected an unplanned or repeated browser tool")
                    observation = await browser.inspect(tool)
                    observations.append(observation)
                    used_tools.add(tool)
                    tool_statuses.append({"tool": tool, "status": str(observation["status"])})
                    if observation["status"] != "completed":
                        raise RuntimeError("browser inspection failed")
        except Exception as exc:  # noqa: BLE001 - browser failures degrade to explicit inconclusive evidence
            logger.warning(
                "case=%s stage=playwright_react status=failed error_type=%s",
                workflow_state.case_id,
                type(exc).__name__,
            )
            workflow_state.record_history(
                "playwright_react",
                "failed",
                f"Bounded browser inspection failed ({type(exc).__name__})",
            )
            state["run_result"] = {
                "status": "react_failed",
                "tool_statuses": tool_statuses,
                "evidence": _observations_text(observations),
                "error_type": type(exc).__name__,
            }
            state["result_status"] = "inconclusive"
            state["result_summary"] = "Browser inspection did not complete; no conclusion was reached."
            return state
        state["observations"] = observations
        state["run_result"] = {
            "status": "completed",
            "tool_statuses": tool_statuses,
            "evidence": _observations_text(observations),
            "rounds": max(0, len(tool_statuses) - 1),
        }
        workflow_state.record_history(
            "playwright_react",
            "completed",
            f"Completed {max(0, len(tool_statuses) - 1)} bounded read-only inspection rounds",
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
        elif result.get("status") == "planning_failed":
            state["result_status"] = "inconclusive"
            state["result_summary"] = "Browser planning failed; no browser tools were run."
        elif result.get("status") == "react_failed":
            state["result_status"] = "inconclusive"
            state["result_summary"] = "Browser validation did not complete; no conclusion was reached."
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
        run_status = run_result.get("status")
        workflow_status = "completed" if run_status == "completed" else "failed"
        if run_status == "planning_failed":
            workflow_status = "skipped"
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
        if ticket_id and run_status not in {"planning_failed", "react_failed"}:
            await context.ticket_flow.add_comment(
                ticket_id,
                f"Playwright validation: {result_status}. {result_summary} (host: {host})",
            )
        return state

    def next_after_revalidation(state: BrowserState) -> str:
        return "plan" if state["host"] is not None else END

    def next_after_plan(state: BrowserState) -> str:
        return "react" if state["run_result"].get("status") != "planning_failed" else "evaluate"

    graph = StateGraph(BrowserState)
    graph.add_node("revalidate", revalidate)
    graph.add_node("plan", plan)
    graph.add_node("react", react)
    graph.add_node("evaluate", evaluate)
    graph.add_node("record", record)
    graph.set_entry_point("revalidate")
    graph.add_conditional_edges("revalidate", next_after_revalidation, {"plan": "plan", END: END})
    graph.add_conditional_edges("plan", next_after_plan, {"react": "react", "evaluate": "evaluate"})
    graph.add_edge("react", "evaluate")
    graph.add_edge("evaluate", "record")
    graph.add_edge("record", END)
    return graph.compile()


def _observations_text(observations: list[dict[str, Any]]) -> str:
    from json import dumps

    return dumps(observations, default=str, ensure_ascii=True)[:12000]
