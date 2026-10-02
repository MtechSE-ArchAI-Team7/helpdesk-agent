from __future__ import annotations

import json
import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from ..config import MCPConfig, TriageConfig
from ..mcp import MCPProxyAdapter
from ..models import WorkflowState
from ..modules.a2a.client import A2AClientManager, DeliveryOutcome
from ..modules.a2a.diagnosis import build_case_input
from ..modules.email.service import ZohoMailFlow
from ..modules.faq.service import FAQFlow
from ..modules.playwright import PlaywrightAssessment, PlaywrightValidationFlow
from ..modules.ticketing.service import JiraTicketFlow, extract_ticket_id
from ..modules.triage.llm import LLMError, LLMTriageClient
from ..modules.triage.schema import TriageResult

logger = logging.getLogger(__name__)

#: Maximum number of clarification rounds before escalating to diagnosis.
MAX_CLARIFICATION_ROUNDS = 2

#: Marker used in Jira comments so clarification rounds can be counted from the ticket.
CLARIFICATION_COMMENT_MARKER = "pending clarifications"

#: Name of the remote diagnosis agent in A2A remote-agent configuration.
DIAGNOSIS_AGENT_NAME = "diagnosis-agent"


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


class HelpdeskWorkflow:
    """LangGraph-based workflow for the helpdesk triage flow."""

    def __init__(
        self,
        mcp_adapter: MCPProxyAdapter | None = None,
        llm_client: LLMTriageClient | None = None,
        triage_config: TriageConfig | None = None,
        a2a_client_manager: A2AClientManager | None = None,
    ) -> None:
        self.mcp_adapter = mcp_adapter
        self.llm_client = llm_client or LLMTriageClient()
        self.triage_config = triage_config or TriageConfig.from_env()
        self.a2a_client_manager = a2a_client_manager
        self.project_key = MCPConfig.from_env().jira_project_key
        self.mail_flow = ZohoMailFlow(mcp_adapter)
        self.ticket_flow = JiraTicketFlow(mcp_adapter)
        self.faq_flow = FAQFlow(mcp_adapter)
        self.playwright_flow = PlaywrightValidationFlow(mcp_adapter)
        self.graph = self._build_graph()
        logger.info(
            "HelpdeskWorkflow initialized: mcp=%s",
            "enabled" if mcp_adapter else "disabled (mocked)",
        )

    def _build_graph(self) -> Any:
        """Build the LangGraph state graph."""
        graph = StateGraph(GraphState)

        # Add nodes
        graph.add_node("load_ticket", self._load_ticket_node)
        graph.add_node("create_ticket", self._create_ticket_node)
        graph.add_node("acknowledge", self._acknowledge_node)
        graph.add_node("triage", self._triage_node)
        graph.add_node("update_ticket", self._update_ticket_node)
        graph.add_node("assess_playwright", self._assess_playwright_node)
        graph.add_node("check_clarification", self._check_clarification_node)
        graph.add_node("clarify", self._clarify_node)
        graph.add_node("playwright_validate", self._playwright_validate_node)
        graph.add_node("faq_gate", self._faq_gate_node)
        graph.add_node("resolve_faq", self._resolve_faq_node)
        graph.add_node("diagnose", self._diagnose_node)
        graph.add_node("email_read", self._email_read_node)

        # Set entry point
        graph.set_entry_point("load_ticket")

        # Add edges
        graph.add_conditional_edges(
            "load_ticket",
            self._has_existing_ticket,
            {
                "existing": "triage",
                "new": "create_ticket",
            },
        )
        graph.add_conditional_edges(
            "create_ticket",
            self._ticket_created,
            {
                "created": "acknowledge",
                "failed": END,
            },
        )
        graph.add_edge("acknowledge", "triage")
        graph.add_edge("triage", "update_ticket")
        graph.add_edge("update_ticket", "assess_playwright")
        graph.add_edge("assess_playwright", "check_clarification")
        graph.add_conditional_edges(
            "check_clarification",
            self._should_clarify,
            {
                "clarify": "clarify",
                "diagnose": "diagnose",
                "playwright": "playwright_validate",
                "faq_gate": "faq_gate",
            },
        )
        graph.add_edge("clarify", END)
        graph.add_edge("playwright_validate", "faq_gate")
        graph.add_conditional_edges(
            "faq_gate",
            self._should_resolve_faq,
            {
                "resolve_faq": "resolve_faq",
                "diagnose": "diagnose",
            },
        )
        graph.add_edge("resolve_faq", END)
        graph.add_edge("diagnose", END)
        graph.add_edge("email_read", END)

        return graph.compile()

    @staticmethod
    def _split_subject_body(initial_input: dict[str, Any]) -> tuple[str, str]:
        """Split the incoming input into (subject, body)."""
        subject = initial_input.get("subject", "")
        summary_text = initial_input.get("summary", "No summary provided")
        parts = summary_text.split("\n\n", 1)
        if not subject:
            subject = parts[0] if parts else "No subject"
        body = parts[1] if len(parts) > 1 else summary_text
        return subject, body

    def _ticketed_subject(self, ticket_id: str | None, subject: str) -> str:
        """Prefix the subject with [TICKET-ID] unless it already contains a
        ticket ID (e.g. a "Re: [AH-23] ..." reply), to avoid stacking prefixes
        like "[AH-23] Re: [AH-23] Issue faced"."""
        if not ticket_id:
            return subject
        if extract_ticket_id(subject, self.project_key):
            return subject
        return f"[{ticket_id}] {subject}"

    @staticmethod
    def _text_to_html(text: str) -> str:
        """Convert plain-text email bodies to simple HTML: paragraphs for
        blank-line-separated blocks and <ul><li> for "- "-prefixed lines, so
        Zoho renders lists instead of one run-on paragraph."""
        import html

        blocks: list[str] = []
        bullets: list[str] = []

        def flush_bullets() -> None:
            if bullets:
                items = "".join(f"<li>{b}</li>" for b in bullets)
                blocks.append(f"<ul>{items}</ul>")
                bullets.clear()

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                flush_bullets()
                continue
            if line.startswith("- "):
                bullets.append(html.escape(line[2:].strip()))
                continue
            flush_bullets()
            blocks.append(f"<p>{html.escape(line)}</p>")
        flush_bullets()
        return "\n".join(blocks)

    async def _load_ticket_node(self, state: GraphState) -> GraphState:
        """Detect a ticket ID in the subject and load the existing Jira ticket."""
        workflow_state = state["workflow_state"]
        initial_input = state["initial_input"]
        subject, _ = self._split_subject_body(initial_input)

        ticket_id = extract_ticket_id(subject, self.project_key)
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

    def _has_existing_ticket(self, state: GraphState) -> str:
        """Route based on whether an existing ticket was loaded."""
        return "existing" if state.get("existing_ticket") else "new"

    def _ticket_created(self, state: GraphState) -> str:
        """Stop the run if ticket creation failed — without a real ticket ID,
        every downstream Jira call and the acknowledgment subject are corrupt."""
        return "created" if state.get("ticket_id") else "failed"

    async def _create_ticket_node(self, state: GraphState) -> GraphState:
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
        logger.info(
            "case=%s stage=create_ticket status=%s ticket_id=%s",
            workflow_state.case_id,
            ticket_result.get("status", "failed"),
            ticket_id,
        )
        return state

    async def _acknowledge_node(self, state: GraphState) -> GraphState:
        """Reply to the user acknowledging the new ticket, with the ticket ID in the subject."""
        workflow_state = state["workflow_state"]
        initial_input = state["initial_input"]
        ticket_id = state.get("ticket_id")
        subject, _ = self._split_subject_body(initial_input)

        logger.info("case=%s stage=acknowledge status=started", workflow_state.case_id)

        ack_subject = self._ticketed_subject(ticket_id, subject)
        ack_body = (
            "Thank you for contacting support.\n\n"
            f"Your request has been logged as ticket {ticket_id or 'N/A'}. "
            "Please keep this ticket ID in the subject line of any follow-up replies "
            "so we can link them to your case.\n\n"
            "Best regards,\nHelpdesk Team"
        )

        message_id = initial_input.get("metadata", {}).get("message_id")
        if message_id:
            email_result = await self.mail_flow.reply_email(
                message_id,
                ack_body,
                subject=ack_subject,
                to=initial_input.get("sender"),
                html_body=self._text_to_html(ack_body),
            )
            workflow_state.record_history(
                "acknowledge",
                email_result.get("status", "failed"),
                f"Acknowledgment sent: {email_result.get('reply_message_id', email_result.get('error', 'unknown'))}",
            )
            logger.info(
                "case=%s stage=acknowledge email_status=%s",
                workflow_state.case_id,
                email_result.get("status", "failed"),
            )
        else:
            logger.info("case=%s stage=acknowledge note=no_message_id_email_not_sent", workflow_state.case_id)
        return state

    async def _triage_node(self, state: GraphState) -> GraphState:
        """Perform LLM triage on the input."""
        workflow_state = state["workflow_state"]
        initial_input = state["initial_input"]
        case_id = workflow_state.case_id

        logger.info("case=%s stage=triage status=started", case_id)

        subject, body = self._split_subject_body(initial_input)

        workflow_state.record_history("triage", "started", f"Received {initial_input.get('source', 'email')} request")

        # For existing tickets, feed the ticket's current state in as context.
        history_snippets: list[str] | None = None
        existing_ticket = state.get("existing_ticket")
        if existing_ticket:
            history_snippets = [
                f"Existing ticket {existing_ticket.get('ticket_id')}: {existing_ticket.get('summary', '')}",
                f"Ticket description: {existing_ticket.get('description', '')}",
            ]
            for comment in existing_ticket.get("comments", []) or []:
                history_snippets.append(f"Ticket comment: {comment}")

        try:
            triage_result = await self.llm_client.triage_email(subject, body, history_snippets=history_snippets)
            state["triage_result"] = triage_result

            workflow_state.triage = {
                "service": triage_result.service,
                "issue_type": triage_result.issue_type,
                "priority": triage_result.priority,
                "confidence": triage_result.confidence,
                "missing_info": triage_result.missing_info,
                "clarifying_questions": triage_result.clarifying_questions,
                "summary": triage_result.summary,
            }
            workflow_state.summary = triage_result.summary
            workflow_state.service = triage_result.service

            workflow_state.record_history(
                "triage",
                "completed",
                f"Classified as {triage_result.issue_type} with priority {triage_result.priority} "
                f"(confidence: {triage_result.confidence:.2f})",
            )
            logger.info(
                "case=%s stage=triage status=completed service=%s issue_type=%s "
                "priority=%s confidence=%.2f missing_info=%d clarifying_questions=%d",
                case_id,
                triage_result.service,
                triage_result.issue_type,
                triage_result.priority,
                triage_result.confidence,
                len(triage_result.missing_info),
                len(triage_result.clarifying_questions),
            )
        except LLMError as e:
            workflow_state.record_history("triage", "failed", f"LLM triage failed: {e}")
            logger.error("case=%s stage=triage status=failed error=%s", case_id, e)
            raise

        return state

    async def _update_ticket_node(self, state: GraphState) -> GraphState:
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

    async def _check_clarification_node(self, state: GraphState) -> GraphState:
        """Check if clarification is needed."""
        # This node just passes through, the decision is made in _should_clarify
        return state

    async def _assess_playwright_node(self, state: GraphState) -> GraphState:
        """Assess browser-test feasibility without invoking browser tools."""
        workflow_state = state["workflow_state"]
        case_id = workflow_state.case_id
        config = self.playwright_flow.config
        skip_reason = None
        if self.mcp_adapter is None:
            skip_reason = "mcp_disabled"
        elif not config.playwright_url:
            skip_reason = "runtime_not_configured"
        elif not config.playwright_allowed_hosts:
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

        subject, body = self._split_subject_body(state["initial_input"])
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
                "Please share the affected page URL and the steps that lead to the error. Do not include passwords or tokens."
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

    def _clarification_rounds(self, state: GraphState) -> int:
        """Count prior clarification rounds from the ticket's comments."""
        existing_ticket = state.get("existing_ticket") or {}
        rounds = 0
        for comment in existing_ticket.get("comments", []) or []:
            text = comment.get("body", "") if isinstance(comment, dict) else str(comment)
            if CLARIFICATION_COMMENT_MARKER in text.lower():
                rounds += 1
        return rounds

    def _should_clarify(self, state: GraphState) -> str:
        """Determine if clarification is needed."""
        case_id = state["workflow_state"].case_id
        triage_result = state.get("triage_result")
        if not triage_result:
            logger.info("case=%s gate=clarification decision=faq_gate reason=no_triage_result", case_id)
            return "faq_gate"

        threshold = self.triage_config.confidence_threshold
        assessment = state.get("playwright_assessment")
        needs_clarification = (
            triage_result.confidence < threshold
            or bool(triage_result.missing_info)
            or bool(assessment and assessment.decision == "needs_info")
        )
        if not needs_clarification:
            if assessment and assessment.decision == "eligible":
                logger.info("case=%s gate=clarification decision=playwright reason=eligible", case_id)
                return "playwright"
            logger.info(
                "case=%s gate=clarification decision=faq_gate reason=confident confidence=%.2f threshold=%.2f",
                case_id,
                triage_result.confidence,
                threshold,
            )
            return "faq_gate"

        rounds = self._clarification_rounds(state)
        if rounds >= MAX_CLARIFICATION_ROUNDS:
            if assessment and assessment.decision == "eligible" and assessment.target_url:
                logger.info(
                    "case=%s gate=clarification decision=playwright reason=max_rounds rounds=%d max=%d",
                    case_id,
                    rounds,
                    MAX_CLARIFICATION_ROUNDS,
                )
                return "playwright"
            logger.info(
                "case=%s gate=clarification decision=diagnose reason=max_rounds rounds=%d max=%d",
                case_id,
                rounds,
                MAX_CLARIFICATION_ROUNDS,
            )
            return "diagnose"

        logger.info(
            "case=%s gate=clarification decision=clarify reason=needs_clarification "
            "confidence=%.2f threshold=%.2f missing_info=%d rounds=%d",
            case_id,
            triage_result.confidence,
            threshold,
            len(triage_result.missing_info),
            rounds,
        )
        return "clarify"

    async def _playwright_validate_node(self, state: GraphState) -> GraphState:
        """Run bounded browser diagnostics, record a concise result, and continue to FAQ."""
        workflow_state = state["workflow_state"]
        assessment = state.get("playwright_assessment")
        subject, body = self._split_subject_body(state["initial_input"])
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

        host, reason = self.playwright_flow.validate_target(assessment.target_url, report)
        if host is None:
            workflow_state.record_history("playwright_validation", "skipped", reason)
            logger.warning(
                "case=%s stage=playwright_validation status=skipped reason=%s",
                workflow_state.case_id,
                reason,
            )
            return state

        started_at = time.monotonic()
        logger.info(
            "case=%s stage=playwright_validation status=started host=%s",
            workflow_state.case_id,
            host,
        )
        run_result = await self.playwright_flow.validate(assessment.target_url)
        for tool_status in run_result.get("tool_statuses", []):
            logger.info(
                "case=%s stage=playwright_validation tool=%s status=%s",
                workflow_state.case_id,
                tool_status.get("tool", "unknown"),
                tool_status.get("status", "unknown"),
            )

        if run_result.get("status") == "completed":
            try:
                evaluation = await self.llm_client.evaluate_playwright_validation(
                    assessment.expected_error,
                    run_result.get("evidence", ""),
                )
                result_status = evaluation.status
                result_summary = evaluation.summary
            except LLMError:
                result_status = "inconclusive"
                result_summary = "Browser evidence was collected but could not be evaluated."
        else:
            result_status = "inconclusive"
            result_summary = "Browser validation did not complete; no conclusion was reached."

        duration_ms = int((time.monotonic() - started_at) * 1000)
        validation = {
            "status": result_status,
            "summary": result_summary,
            "host": host,
            "tool_statuses": run_result.get("tool_statuses", []),
        }
        state["playwright_validation"] = validation
        workflow_state.metadata["playwright_validation"] = validation
        workflow_state.evidence.append(
            {
                "source": "playwright",
                "status": result_status,
                "summary": result_summary,
                "host": host,
            }
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

        ticket_id = state.get("ticket_id")
        if ticket_id:
            await self.ticket_flow.add_comment(
                ticket_id,
                f"Playwright validation: {result_status}. {result_summary} (host: {host})",
            )
        return state

    async def _clarify_node(self, state: GraphState) -> GraphState:
        """Send clarification questions to the user and mark the ticket pending clarifications."""
        workflow_state = state["workflow_state"]
        triage_result = state.get("triage_result")
        initial_input = state["initial_input"]
        ticket_id = state.get("ticket_id")
        case_id = workflow_state.case_id

        logger.info("case=%s stage=clarify status=started", case_id)

        if triage_result is None:
            workflow_state.record_history("clarify", "failed", "No triage result available")
            logger.error("case=%s stage=clarify status=failed error=no_triage_result", case_id)
            return state

        # Draft clarification email
        clarification_body = await self.llm_client.draft_clarification(triage_result, triage_result.raw_text)

        subject, _ = self._split_subject_body(initial_input)
        reply_subject = self._ticketed_subject(ticket_id, subject)

        # Reply to the user with the clarifying questions
        message_id = initial_input.get("metadata", {}).get("message_id")
        if message_id:
            email_result = await self.mail_flow.reply_email(
                message_id,
                clarification_body,
                subject=reply_subject,
                to=initial_input.get("sender"),
                html_body=self._text_to_html(clarification_body),
            )
            workflow_state.record_history(
                "clarify",
                email_result.get("status", "failed"),
                f"Clarification email sent: {email_result.get('reply_message_id', email_result.get('error', 'unknown'))}",
            )
            logger.info(
                "case=%s stage=clarify email_status=%s",
                case_id,
                email_result.get("status", "failed"),
            )
        else:
            logger.info("case=%s stage=clarify note=no_message_id_email_not_sent", case_id)

        # Update the Jira ticket as pending clarifications
        if ticket_id:
            questions = "\n".join(f"- {q}" for q in triage_result.clarifying_questions) or "- (general details requested)"
            comment_body = (
                f"Pending clarifications — questions sent to the requester:\n{questions}\n\n"
                f"Missing info: {', '.join(triage_result.missing_info) or 'n/a'}"
            )
            comment_result = await self.ticket_flow.add_comment(ticket_id, comment_body)
            workflow_state.record_history(
                "clarify",
                comment_result.get("status", "failed"),
                f"Ticket comment added: pending clarifications",
            )
            logger.info(
                "case=%s stage=clarify comment_status=%s ticket_id=%s",
                case_id,
                comment_result.get("status", "failed"),
                ticket_id,
            )

        workflow_state.current_step = "awaiting_clarification"
        workflow_state.clarification = {
            "questions": triage_result.clarifying_questions,
            "missing_info": triage_result.missing_info,
        }

        return state

    async def _faq_gate_node(self, state: GraphState) -> GraphState:
        """Check FAQ for existing solution."""
        workflow_state = state["workflow_state"]
        triage_result = state.get("triage_result")
        case_id = workflow_state.case_id

        logger.info("case=%s stage=faq_gate status=started", case_id)

        if triage_result is None:
            workflow_state.record_history("faq_gate", "failed", "No triage result available")
            logger.error("case=%s stage=faq_gate status=failed error=no_triage_result", case_id)
            return state

        workflow_state.record_history("faq_gate", "started", "Searching FAQ repository")

        # Search FAQ
        faq_results = await self.faq_flow.search(triage_result.summary, max_results=5)
        state["faq_results"] = faq_results

        if faq_results.get("status") == "success":
            workflow_state.faq_matches = faq_results.get("results", [])
            workflow_state.record_history(
                "faq_gate",
                "completed",
                f"Found {faq_results.get('count', 0)} FAQ matches",
            )
            logger.info(
                "case=%s stage=faq_gate status=completed matches=%d",
                case_id,
                faq_results.get("count", 0),
            )
        else:
            workflow_state.record_history(
                "faq_gate", "failed", f"FAQ search failed: {faq_results.get('error', 'unknown')}"
            )
            logger.warning(
                "case=%s stage=faq_gate status=failed error=%s",
                case_id,
                faq_results.get("error", "unknown"),
            )

        return state

    def _should_resolve_faq(self, state: GraphState) -> str:
        """Determine if FAQ solution should be used."""
        case_id = state["workflow_state"].case_id
        faq_results = state.get("faq_results") or {}
        if faq_results.get("status") != "success":
            logger.info("case=%s gate=faq decision=diagnose reason=faq_search_failed", case_id)
            return "diagnose"

        results = faq_results.get("results", [])
        if not results:
            logger.info("case=%s gate=faq decision=diagnose reason=no_faq_matches", case_id)
            return "diagnose"

        # Check relevance threshold
        threshold = self.triage_config.faq_relevance_threshold
        top_result = results[0]
        if top_result.get("score", 0) >= threshold:
            logger.info(
                "case=%s gate=faq decision=resolve_faq reason=relevant_match score=%.3f threshold=%.2f",
                case_id,
                top_result.get("score", 0),
                threshold,
            )
            return "resolve_faq"
        logger.info(
            "case=%s gate=faq decision=diagnose reason=below_threshold score=%.3f threshold=%.2f",
            case_id,
            top_result.get("score", 0),
            threshold,
        )
        return "diagnose"

    async def _resolve_faq_node(self, state: GraphState) -> GraphState:
        """Resolve with FAQ solution: reply to the user and comment on the ticket."""
        workflow_state = state["workflow_state"]
        faq_results = state.get("faq_results")
        initial_input = state["initial_input"]
        ticket_id = state.get("ticket_id")
        case_id = workflow_state.case_id

        logger.info("case=%s stage=resolve_faq status=started", case_id)

        if faq_results is None or not faq_results.get("results"):
            workflow_state.record_history("resolve_faq", "failed", "No FAQ results available")
            logger.error("case=%s stage=resolve_faq status=failed error=no_faq_results", case_id)
            return state

        top_result = faq_results["results"][0]

        # Draft resolution email
        resolution_body = await self.llm_client.draft_resolution(top_result, workflow_state.summary)

        subject, _ = self._split_subject_body(initial_input)
        reply_subject = self._ticketed_subject(ticket_id, subject)

        # Reply to the user with the FAQ answer
        message_id = initial_input.get("metadata", {}).get("message_id")
        if message_id:
            email_result = await self.mail_flow.reply_email(
                message_id,
                resolution_body,
                subject=reply_subject,
                to=initial_input.get("sender"),
                html_body=self._text_to_html(resolution_body),
            )
            workflow_state.record_history(
                "resolve_faq",
                email_result.get("status", "failed"),
                f"Resolution email sent: {email_result.get('reply_message_id', email_result.get('error', 'unknown'))}",
            )
            logger.info(
                "case=%s stage=resolve_faq email_status=%s",
                case_id,
                email_result.get("status", "failed"),
            )

        # Update the Jira ticket: FAQ answer provided
        if ticket_id:
            comment_body = (
                f"FAQ answer provided to the requester.\n\n"
                f"Matched FAQ: {top_result.get('title', 'unknown')} "
                f"(score: {top_result.get('score', 0):.3f})"
            )
            comment_result = await self.ticket_flow.add_comment(ticket_id, comment_body)
            workflow_state.record_history(
                "resolve_faq",
                comment_result.get("status", "failed"),
                "Ticket comment added: FAQ answer provided",
            )
            logger.info(
                "case=%s stage=resolve_faq comment_status=%s ticket_id=%s",
                case_id,
                comment_result.get("status", "failed"),
                ticket_id,
            )

        workflow_state.current_step = "faq_answer_provided"
        logger.info("case=%s stage=resolve_faq status=completed", case_id)
        return state

    async def _diagnose_node(self, state: GraphState) -> GraphState:
        """Mark the ticket as requiring diagnosis (no FAQ match or clarification exhausted)."""
        workflow_state = state["workflow_state"]
        triage_result = state.get("triage_result")
        ticket_id = state.get("ticket_id")
        case_id = workflow_state.case_id

        logger.info("case=%s stage=diagnose status=started", case_id)

        rounds = self._clarification_rounds(state)
        triage_summary = triage_result.summary if triage_result else workflow_state.summary

        if rounds >= MAX_CLARIFICATION_ROUNDS:
            comment_body = (
                f"Diagnosis required — clarification attempts exhausted "
                f"({rounds}/{MAX_CLARIFICATION_ROUNDS} rounds).\n\n"
                f"Latest triage summary: {triage_summary}"
            )
        else:
            comment_body = (
                "Diagnosis required — no matching FAQ answer found.\n\n"
                f"Triage summary: {triage_summary}"
            )

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

    async def _trigger_diagnosis_agent(self, state: GraphState) -> None:
        """Fire-and-forget hand-off of the case to the diagnosis agent (Agent 2).

        The diagnosis agent runs the investigation asynchronously and delivers
        results (e.g. to the Jira ticket) itself; this workflow does not poll.
        Any hand-off failure is logged and recorded but never fails the run.
        """
        workflow_state = state["workflow_state"]
        case_id = workflow_state.case_id
        manager = self.a2a_client_manager

        if manager is None or not manager.has_target(DIAGNOSIS_AGENT_NAME):
            workflow_state.record_history(
                "diagnose", "skipped", f"Remote agent {DIAGNOSIS_AGENT_NAME!r} not configured"
            )
            return

        case_payload = build_case_input(workflow_state, state.get("triage_result"), state.get("ticket_id"))
        logger.info(
            "case=%s stage=diagnose handoff_payload=%s",
            case_id,
            json.dumps(case_payload, default=str),
        )
        try:
            outcome = await manager.send_case(DIAGNOSIS_AGENT_NAME, case_payload, context_id=case_id)
        except Exception as exc:  # noqa: BLE001 - escalation must never sink the workflow
            logger.exception("case=%s stage=diagnose handoff_error", case_id)
            outcome = DeliveryOutcome(submitted=False, reason=f"unexpected error: {exc.__class__.__name__}")

        workflow_state.metadata["diagnosis_trigger"] = {
            "agent": DIAGNOSIS_AGENT_NAME,
            "submitted": outcome.submitted,
            "task_id": outcome.task_id,
            "run_id": outcome.run_id,
            "reason": outcome.reason,
        }
        workflow_state.record_history(
            "diagnose",
            "handoff_submitted" if outcome.submitted else "handoff_failed",
            f"Diagnosis hand-off to {DIAGNOSIS_AGENT_NAME}: {'submitted' if outcome.submitted else outcome.reason}",
        )
        logger.info(
            "case=%s stage=diagnose handoff_submitted=%s reason=%s",
            case_id,
            outcome.submitted,
            outcome.reason,
        )

        ticket_id = state.get("ticket_id")
        if not outcome.submitted and ticket_id:
            comment_result = await self.ticket_flow.add_comment(
                ticket_id,
                "Diagnosis agent unreachable — automated investigation could not be started "
                f"({outcome.reason}). Manual investigation required.",
            )
            workflow_state.record_history(
                "diagnose",
                comment_result.get("status", "failed"),
                "Ticket comment added: diagnosis agent unreachable",
            )

    async def _email_read_node(self, state: GraphState) -> GraphState:
        """Handle email read requests."""
        workflow_state = state["workflow_state"]

        workflow_state.record_history("email_read", "started", "Fetching messages from Zoho Mail")
        email_result = await self.mail_flow.get_messages(query="is:unread", max_results=10)
        messages_summary = f"Retrieved {email_result.get('count', 0)} unread messages"
        workflow_state.record_history("email_read", "completed", messages_summary)
        workflow_state.current_step = "email_retrieved"
        workflow_state.metadata["email_messages"] = email_result.get("messages", [])

        return state

    async def run(self, initial_state: dict[str, Any]) -> WorkflowState:
        """Run the workflow with the given initial state."""
        # Create new workflow state (Jira is the source of truth; nothing is resumed
        # from local state — existing tickets are re-fetched by ID from the subject).
        workflow_state = WorkflowState(
            case_id=initial_state.get("case_id", "case-unknown"),
            summary=initial_state.get("summary", "No summary provided"),
            service="unknown",
            metadata=initial_state.get("metadata", {}),
        )
        workflow_state.add_message("user", initial_state.get("summary", ""))

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

                email_result = await self._email_read_node(email_read_state)
                return email_result["workflow_state"]

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
        graph_result: GraphState = await self.graph.ainvoke(graph_state)
        final_state = graph_result["workflow_state"]
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
