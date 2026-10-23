from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from helpdesk_agent.application.communication import _text_to_html
from helpdesk_agent.application.memory import _memory_failure
from helpdesk_agent.config.settings import TriageConfig
from helpdesk_agent.domain.exceptions import MemoryError
from helpdesk_agent.domain.models.browser import BrowserSettings
from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.domain.policies.triage import CLARIFICATION_COMMENT_MARKER as CLARIFICATION_COMMENT_MARKER
from helpdesk_agent.domain.policies.triage import DIAGNOSIS_AGENT_NAME as DIAGNOSIS_AGENT_NAME
from helpdesk_agent.observability.tracing import emit_workflow_event
from helpdesk_agent.observability.workflow import _trace_step
from helpdesk_agent.ports.capabilities import (
    BrowserPort,
    DiagnosisPort,
    EmailPort,
    KnowledgePort,
    MemoryPort,
    ReasoningPort,
    TicketingPort,
)
from helpdesk_agent.workflows.browser.graph import build_browser_graph
from helpdesk_agent.workflows.browser.nodes import _assess_playwright_node, _browser_report, _playwright_validate_node
from helpdesk_agent.workflows.graph import build_graph
from helpdesk_agent.workflows.helpers import (
    _save_incoming_memory,
    _save_ticket_memory,
    _split_subject_body,
    _ticketed_subject,
    save_chat_response,
)
from helpdesk_agent.workflows.nodes.communication import (
    _acknowledge_node,
    _clarify_node,
    _email_read_node,
    _resolve_faq_node,
)
from helpdesk_agent.workflows.nodes.diagnosis import _diagnose_node, _trigger_diagnosis_agent
from helpdesk_agent.workflows.nodes.tickets import _create_ticket_node, _load_ticket_node, _update_ticket_node
from helpdesk_agent.workflows.nodes.triage import _check_clarification_node, _faq_gate_node, _triage_node
from helpdesk_agent.workflows.routing import (
    _clarification_rounds,
    _has_existing_ticket,
    _should_clarify,
    _should_resolve_faq,
    _ticket_created,
)
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


class HelpdeskWorkflow:
    def __init__(
        self,
        *,
        llm_client: ReasoningPort,
        mail_flow: EmailPort,
        ticket_flow: TicketingPort,
        faq_flow: KnowledgePort,
        playwright_flow: BrowserPort,
        triage_config: TriageConfig,
        project_key: str,
        mcp_enabled: bool,
        browser_settings: BrowserSettings,
        a2a_client_manager: DiagnosisPort | None = None,
        memory: MemoryPort | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.mail_flow = mail_flow
        self.ticket_flow = ticket_flow
        self.faq_flow = faq_flow
        self.playwright_flow = playwright_flow
        self.triage_config = triage_config
        self.project_key = project_key
        self.mcp_enabled = mcp_enabled
        self.browser_settings = browser_settings
        self.a2a_client_manager = a2a_client_manager
        self.memory = memory
        self.browser_graph = build_browser_graph(self)
        self.graph = self._build_graph()
        logger.info("HelpdeskWorkflow initialized: mcp=%s", "enabled" if mcp_enabled else "disabled (mocked)")

    def _build_graph(self) -> Any:
        return build_graph(self)

    async def _load_ticket_node(self, state: GraphState) -> GraphState:
        return await _load_ticket_node(self, state)

    async def _create_ticket_node(self, state: GraphState) -> GraphState:
        return await _create_ticket_node(self, state)

    async def _update_ticket_node(self, state: GraphState) -> GraphState:
        return await _update_ticket_node(self, state)

    async def _acknowledge_node(self, state: GraphState) -> GraphState:
        return await _acknowledge_node(self, state)

    async def _clarify_node(self, state: GraphState) -> GraphState:
        return await _clarify_node(self, state)

    async def _resolve_faq_node(self, state: GraphState) -> GraphState:
        return await _resolve_faq_node(self, state)

    async def _email_read_node(self, state: GraphState) -> GraphState:
        return await _email_read_node(self, state)

    async def _triage_node(self, state: GraphState) -> GraphState:
        return await _triage_node(self, state)

    async def _check_clarification_node(self, state: GraphState) -> GraphState:
        return await _check_clarification_node(self, state)

    async def _faq_gate_node(self, state: GraphState) -> GraphState:
        return await _faq_gate_node(self, state)

    async def _diagnose_node(self, state: GraphState) -> GraphState:
        return await _diagnose_node(self, state)

    async def _trigger_diagnosis_agent(self, state: GraphState) -> None:
        return await _trigger_diagnosis_agent(self, state)

    async def _assess_playwright_node(self, state: GraphState) -> GraphState:
        return await _assess_playwright_node(self, state)

    async def _playwright_validate_node(self, state: GraphState) -> GraphState:
        return await _playwright_validate_node(self, state)

    def _browser_report(self, state: GraphState) -> tuple[str, str]:
        return _browser_report(self, state)

    def _has_existing_ticket(self, state: GraphState) -> str:
        return _has_existing_ticket(self, state)

    def _ticket_created(self, state: GraphState) -> str:
        return _ticket_created(self, state)

    def _clarification_rounds(self, state: GraphState) -> int:
        return _clarification_rounds(self, state)

    def _should_clarify(self, state: GraphState) -> str:
        return _should_clarify(self, state)

    def _should_resolve_faq(self, state: GraphState) -> str:
        return _should_resolve_faq(self, state)

    @staticmethod
    def _split_subject_body(initial_input: dict[str, Any]) -> tuple[str, str]:
        return _split_subject_body(initial_input)

    def _ticketed_subject(self, ticket_id: str | None, subject: str) -> str:
        return _ticketed_subject(self, ticket_id, subject)

    @staticmethod
    def _text_to_html(text: str) -> str:
        return _text_to_html(text)

    @staticmethod
    def _memory_failure(state: WorkflowState, step: str, error: MemoryError) -> None:
        return _memory_failure(state, step, error)

    async def save_chat_response(self, state: WorkflowState, content: str, identity: str) -> None:
        return await save_chat_response(self, state, content, identity)

    async def _save_incoming_memory(self, graph_state: GraphState, ticket_id: str) -> None:
        return await _save_incoming_memory(self, graph_state, ticket_id)

    async def _save_ticket_memory(self, graph_state: GraphState) -> None:
        return await _save_ticket_memory(self, graph_state)

    @_trace_step("helpdesk.workflow")
    async def run(self, initial_state: dict[str, Any]) -> WorkflowState:
        """Run the workflow with the given initial state."""
        # Create new workflow state (Jira is the source of truth; nothing is resumed
        # from local state — existing tickets are re-fetched by ID from the subject).
        initial_state = dict(initial_state)
        if self.memory:
            initial_state["memory_identity"] = str(
                initial_state.get("metadata", {}).get("message_id")
                or initial_state.get("memory_identity")
                or uuid4().hex
            )
        workflow_state = WorkflowState(
            case_id=initial_state.get("case_id", "case-unknown"),
            summary=initial_state.get("summary", "No summary provided"),
            service="unknown",
            metadata=initial_state.get("metadata", {}),
        )
        workflow_state.add_message("user", initial_state.get("summary", ""))
        event_source = initial_state.get("source")
        emit_workflow_event(
            workflow_state,
            event_type="run.started",
            stage="intake",
            actor_type="DETERMINISTIC",
            status="STARTED",
            summary="Helpdesk run started",
            details={
                "source": (
                    event_source
                    if isinstance(event_source, str) and event_source in {"email", "chat", "a2a"}
                    else "unknown"
                )
            },
        )

        case_id = workflow_state.case_id
        logger.info(
            "case=%s run=started source=%s",
            case_id,
            initial_state.get("source", "unknown"),
        )

        # Check if this is an email read request (bypasses triage). Only chat /
        # A2A users can issue this command; inbound emails (source="email") are
        # always triaged, since an email body mentioning e.g. "get" and "mail"
        # is a support request, not a mailbox-read command.
        source = initial_state.get("source", "unknown")
        if source in ("chat", "a2a"):
            command_text = (initial_state.get("subject") or initial_state.get("summary", "")).lower()
            if any(keyword in command_text for keyword in ["read", "get", "fetch", "retrieve", "show", "list"]) and any(
                keyword in command_text for keyword in ["email", "mail", "message"]
            ):
                logger.info("case=%s run=email_read bypassing triage", case_id)
                email_read_state: GraphState = {
                    "workflow_state": workflow_state,
                    "initial_input": initial_state,
                    "triage_result": None,
                    "faq_results": None,
                    "ticket_id": None,
                    "existing_ticket": None,
                    "playwright_assessment": None,
                    "playwright_validation": None,
                }

                try:
                    email_result = await self._email_read_node(email_read_state)
                except Exception as exc:
                    emit_workflow_event(
                        workflow_state,
                        event_type="run.finished",
                        stage="intake",
                        actor_type="DETERMINISTIC",
                        status="FAILED",
                        summary="Helpdesk run failed",
                        details={"failure_type": type(exc).__name__},
                    )
                    raise
                final_state = email_result["workflow_state"]
                emit_workflow_event(
                    final_state,
                    event_type="run.finished",
                    stage="intake",
                    actor_type="DETERMINISTIC",
                    status="COMPLETED",
                    summary="Helpdesk run completed",
                    details={"terminal_outcome": final_state.current_step},
                )
                return final_state

        # Normal flow: detect/load ticket, then triage
        graph_state: GraphState = {
            "workflow_state": workflow_state,
            "initial_input": initial_state,
            "triage_result": None,
            "faq_results": None,
            "ticket_id": None,
            "existing_ticket": None,
            "playwright_assessment": None,
            "playwright_validation": None,
        }

        # Run the graph
        try:
            graph_result: GraphState = await self.graph.ainvoke(graph_state)
        except Exception as exc:
            emit_workflow_event(
                workflow_state,
                event_type="run.finished",
                stage="intake",
                actor_type="DETERMINISTIC",
                status="FAILED",
                summary="Helpdesk run failed",
                details={"failure_type": type(exc).__name__},
            )
            raise
        final_state = graph_result["workflow_state"]
        if self.memory and graph_result.get("ticket_id"):
            await self._save_ticket_memory(graph_result)
        emit_workflow_event(
            final_state,
            event_type="run.finished",
            stage="intake",
            actor_type="DETERMINISTIC",
            status="COMPLETED",
            summary="Helpdesk run completed",
            details={"terminal_outcome": final_state.current_step},
        )
        logger.info(
            "case=%s run=finished current_step=%s ticket=%s",
            final_state.case_id,
            final_state.current_step,
            (final_state.ticket or {}).get("ticket_id", "none"),
        )
        return final_state

    def create_langgraph_plan(self) -> list[str]:
        return [
            "load_ticket",
            "create_ticket",
            "acknowledge",
            "triage",
            "update_ticket",
            "assess_playwright",
            "clarify",
            "playwright_validate",
            "faq_gate",
            "resolve_faq",
            "diagnose",
        ]
