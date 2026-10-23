from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from opentelemetry.trace import Status, StatusCode

from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.observability.tracing import capture_content, emit_workflow_event, operation_span

logger = logging.getLogger(__name__)
_EVENT_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


TraceParameters = ParamSpec("TraceParameters")
TraceOutput = TypeVar("TraceOutput")


def _trace_step(
    name: str,
) -> Callable[
    [Callable[TraceParameters, Coroutine[Any, Any, TraceOutput]]],
    Callable[TraceParameters, Coroutine[Any, Any, TraceOutput]],
]:
    def decorate(
        function: Callable[TraceParameters, Coroutine[Any, Any, TraceOutput]],
    ) -> Callable[TraceParameters, Coroutine[Any, Any, TraceOutput]]:
        @wraps(function)
        async def wrapped(*args: TraceParameters.args, **kwargs: TraceParameters.kwargs) -> TraceOutput:
            state = args[1] if len(args) > 1 else kwargs.get("state", kwargs.get("initial_state"))
            initial = state if isinstance(state, dict) else {}
            previous = initial.get("workflow_state")
            history_start = len(previous.history) if isinstance(previous, WorkflowState) else 0
            request = initial.get("initial_input", initial)
            session_id = str(initial.get("case_id", "case-unknown")) if name == "helpdesk.workflow" else None
            with operation_span(name, session_id) as span:
                span.set_attribute("langfuse.observation.type", "agent" if name == "helpdesk.workflow" else "span")
                capture_content(
                    span,
                    "input",
                    {key: request[key] for key in ("subject", "body", "summary", "source") if key in request},
                )
                stage = name.removeprefix("workflow.")
                event_state = previous if isinstance(previous, WorkflowState) else None
                actor_type = _event_actor_type(stage)
                started = time.perf_counter()
                if event_state is not None:
                    emit_workflow_event(
                        event_state,
                        event_type="step.started",
                        stage=stage,
                        actor_type=actor_type,
                        status="STARTED",
                        summary=f"{stage.replace('_', ' ').title()} started",
                    )
                try:
                    result = await function(*args, **kwargs)
                except Exception as exc:
                    if event_state is not None:
                        emit_workflow_event(
                            event_state,
                            event_type="step.finished",
                            stage=stage,
                            actor_type=actor_type,
                            status="FAILED",
                            summary=f"{stage.replace('_', ' ').title()} failed",
                            duration_ms=round((time.perf_counter() - started) * 1000),
                            details={"failure_type": type(exc).__name__},
                        )
                    raise
                current = result.get("workflow_state") if isinstance(result, dict) else result
                if isinstance(current, WorkflowState):
                    span.set_attribute("helpdesk.case_id", current.case_id)
                    ticket_id = (current.ticket or {}).get("ticket_id")
                    if ticket_id:
                        span.set_attribute("langfuse.observation.metadata.ticket_id", str(ticket_id))
                    capture_content(
                        span,
                        "output",
                        {
                            "case_id": current.case_id,
                            "current_step": current.current_step,
                            "summary": current.summary,
                            "triage": current.triage,
                            "clarification": current.clarification,
                            "messages": current.messages,
                        },
                    )
                    if any(entry["status"] == "failed" for entry in current.history[history_start:]):
                        span.set_status(Status(StatusCode.ERROR, "Workflow operation reported failure"))
                    history = current.history[history_start:]
                    status = "FAILED" if any(entry["status"] == "failed" for entry in history) else "COMPLETED"
                    if history and all(entry["status"] == "skipped" for entry in history):
                        status = "SKIPPED"
                    details: dict[str, Any] = {
                        "current_step": current.current_step,
                        "history_entries": len(history),
                        "operation_statuses": [
                            f"{entry['step']}={entry['status']}"
                            for entry in history
                            if entry.get("step")
                            in {
                                "load_ticket",
                                "create_ticket",
                                "acknowledge",
                                "triage",
                                "update_ticket",
                                "playwright_assessment",
                                "playwright_validation",
                                "clarify",
                                "faq_gate",
                                "resolve_faq",
                                "diagnose",
                                "email_read",
                                "memory_history",
                                "memory_similar",
                                "memory_save_user",
                                "memory_snapshot",
                            }
                            and entry.get("status")
                            in {
                                "started",
                                "completed",
                                "failed",
                                "skipped",
                                "mocked",
                                "created",
                                "updated",
                                "commented",
                                "sent",
                                "handoff_submitted",
                                "handoff_failed",
                                "success",
                            }
                        ],
                    }
                    if ticket_id:
                        details["ticket_id"] = ticket_id
                    if stage == "triage" and current.triage:
                        details.update(
                            {
                                key: current.triage[key]
                                for key in ("service", "issue_type", "priority", "confidence")
                                if key in current.triage
                            }
                        )
                        details["missing_info_count"] = len(current.triage.get("missing_info", []))
                    elif stage == "faq_gate":
                        details["faq_result_count"] = len(current.faq_matches)
                    elif stage == "assess_playwright" and isinstance(result, dict):
                        assessment = result.get("playwright_assessment")
                        if assessment is not None:
                            details["browser_decision"] = assessment.decision
                    elif stage == "playwright_validate" and isinstance(result, dict):
                        validation = result.get("playwright_validation")
                        if isinstance(validation, dict):
                            if validation.get("status") in {"completed", "failed"}:
                                details["browser_status"] = validation["status"]
                            details["tool_statuses"] = [
                                f"{item.get('tool')}={item.get('status')}"
                                for item in validation.get("tool_statuses", [])
                                if isinstance(item, dict)
                                and item.get("tool")
                                in {
                                    "browser_navigate",
                                    "browser_snapshot",
                                    "browser_console_messages",
                                    "browser_network_requests",
                                }
                                and item.get("status") in {"completed", "failed"}
                            ]
                    elif stage == "diagnose":
                        handoff = current.metadata.get("diagnosis_trigger", {})
                        if isinstance(handoff, dict):
                            if isinstance(handoff.get("submitted"), bool):
                                details["handoff_submitted"] = handoff["submitted"]
                            for key in ("task_id", "run_id"):
                                value = handoff.get(key)
                                if isinstance(value, str) and _EVENT_IDENTIFIER.fullmatch(value):
                                    details[f"diagnosis_{key}"] = value
                    elif stage == "helpdesk.workflow":
                        details["terminal_outcome"] = current.current_step
                    if stage != "helpdesk.workflow":
                        emit_workflow_event(
                            current,
                            event_type="step.finished",
                            stage=stage,
                            actor_type=actor_type,
                            status=status,
                            summary=f"{stage.replace('_', ' ').title()} {status.lower()}",
                            duration_ms=round((time.perf_counter() - started) * 1000),
                            details=details,
                        )
                return result

        return wrapped

    return decorate


def _event_actor_type(stage: str) -> str:
    if stage in {"triage", "assess_playwright", "clarify", "resolve_faq"}:
        return "MODEL_ASSISTED"
    if stage in {
        "load_ticket",
        "create_ticket",
        "acknowledge",
        "update_ticket",
        "playwright_validate",
        "faq_gate",
        "diagnose",
        "email_read",
    }:
        return "TOOL"
    return "DETERMINISTIC"
