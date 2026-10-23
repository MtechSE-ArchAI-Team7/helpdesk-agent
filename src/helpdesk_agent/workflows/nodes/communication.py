from __future__ import annotations

import logging

from helpdesk_agent.domain.exceptions import LLMError
from helpdesk_agent.observability.workflow import _trace_step
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


@_trace_step("workflow.acknowledge")
async def _acknowledge_node(self: WorkflowContext, state: GraphState) -> GraphState:
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
        if email_result.get("status") == "sent":
            workflow_state.add_message("assistant", ack_body)
            await self.save_chat_response(
                workflow_state, ack_body, f"{initial_input.get('memory_identity', '')}:acknowledge"
            )
    else:
        logger.info("case=%s stage=acknowledge note=no_message_id_email_not_sent", workflow_state.case_id)
    return state


@_trace_step("workflow.clarify")
async def _clarify_node(self: WorkflowContext, state: GraphState) -> GraphState:
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

    subject, _ = self._split_subject_body(initial_input)
    reply_subject = self._ticketed_subject(ticket_id, subject)

    # Reply to the user with the clarifying questions
    message_id = initial_input.get("metadata", {}).get("message_id")
    if message_id:
        try:
            clarification_body = await self.llm_client.draft_clarification(triage_result, triage_result.raw_text)
            workflow_state.record_history("email_generation", "completed", "Generated clarification email draft")
        except LLMError as exc:
            logger.warning(
                "case=%s stage=email_generation status=failed type=clarification error_type=%s",
                case_id,
                type(exc).__name__,
            )
            workflow_state.record_history(
                "email_generation", "failed", f"Using deterministic clarification template ({type(exc).__name__})"
            )
            clarification_body = self.llm_client.fallback_clarification(triage_result, triage_result.raw_text)
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
        if email_result.get("status") == "sent":
            workflow_state.add_message("assistant", clarification_body)
            await self.save_chat_response(
                workflow_state, clarification_body, f"{initial_input.get('memory_identity', '')}:clarify"
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
            "Ticket comment added: pending clarifications",
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


@_trace_step("workflow.resolve_faq")
async def _resolve_faq_node(self: WorkflowContext, state: GraphState) -> GraphState:
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

    subject, _ = self._split_subject_body(initial_input)
    reply_subject = self._ticketed_subject(ticket_id, subject)

    # Reply to the user with the FAQ answer
    message_id = initial_input.get("metadata", {}).get("message_id")
    if message_id:
        try:
            resolution_body = await self.llm_client.draft_resolution(top_result, workflow_state.summary)
            workflow_state.record_history("email_generation", "completed", "Generated resolution email draft")
        except LLMError as exc:
            logger.warning(
                "case=%s stage=email_generation status=failed type=resolution error_type=%s",
                case_id,
                type(exc).__name__,
            )
            workflow_state.record_history(
                "email_generation", "failed", f"Using deterministic resolution template ({type(exc).__name__})"
            )
            resolution_body = self.llm_client.fallback_resolution(top_result, workflow_state.summary)
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
        if email_result.get("status") == "sent":
            workflow_state.add_message("assistant", resolution_body)
            await self.save_chat_response(
                workflow_state, resolution_body, f"{initial_input.get('memory_identity', '')}:resolve_faq"
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


@_trace_step("workflow.email_read")
async def _email_read_node(self: WorkflowContext, state: GraphState) -> GraphState:
    """Handle email read requests."""
    workflow_state = state["workflow_state"]

    workflow_state.record_history("email_read", "started", "Fetching messages from Zoho Mail")
    email_result = await self.mail_flow.get_messages(query="is:unread", max_results=10)
    messages_summary = f"Retrieved {email_result.get('count', 0)} unread messages"
    workflow_state.record_history("email_read", "completed", messages_summary)
    workflow_state.current_step = "email_retrieved"
    workflow_state.metadata["email_messages"] = email_result.get("messages", [])

    return state
