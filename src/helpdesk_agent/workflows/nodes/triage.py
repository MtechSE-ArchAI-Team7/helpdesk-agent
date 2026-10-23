from __future__ import annotations

import logging

from helpdesk_agent.application.context import bounded_snippets
from helpdesk_agent.domain.exceptions import LLMError, MemoryError
from helpdesk_agent.domain.policies.triage import apply_triage_reflection
from helpdesk_agent.observability.workflow import _trace_step
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


@_trace_step("workflow.triage")
async def _triage_node(self: WorkflowContext, state: GraphState) -> GraphState:
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

    if self.memory:
        identity = initial_input["memory_identity"]
        ticket_id = state.get("ticket_id")
        if ticket_id:
            conversation_id = initial_input.get("metadata", {}).get("conversation_id")
            if conversation_id:
                # Preserve the link even if later triage or delivery fails.
                await self.memory.bind_conversation(conversation_id, ticket_id)
            # Retrieve before writing so the new user message is not duplicated in context.
            try:
                memory_history = await self.memory.history(ticket_id, identity)
                workflow_state.metadata["ticket_memory"] = memory_history
                history_snippets = [
                    f"Previous conversation on this ticket:\n{snippet}" for snippet in memory_history
                ] + (history_snippets or [])
                workflow_state.record_history("memory_history", "completed", f"Loaded {len(memory_history)} turns")
            except MemoryError as exc:
                self._memory_failure(workflow_state, "memory_history", exc)
            if not initial_input.get("memory_user_saved"):
                await self._save_incoming_memory(state, ticket_id)
            history_snippets = bounded_snippets(history_snippets or [], self.memory.context_chars)
            try:
                query_parts = [subject, body]
                if existing_ticket:
                    query_parts.extend(
                        [str(existing_ticket.get("summary", "")), str(existing_ticket.get("description", ""))]
                    )
                query_parts.extend(workflow_state.metadata.get("ticket_memory", []))
                similar = await self.memory.similar(ticket_id, "\n".join(query_parts))
                workflow_state.metadata["similar_cases"] = similar
                history_snippets = (history_snippets or []) + [
                    f"OTHER ticket {case['ticket_id']} (not this request): {case['qualification']}\n{case['content']}"
                    for case in similar
                ]
                workflow_state.record_history("memory_similar", "completed", f"Found {len(similar)} similar tickets")
            except MemoryError as exc:
                self._memory_failure(workflow_state, "memory_similar", exc)
            # Independent budgets keep long Jira comments from crowding out similar cases.
            history_snippets = bounded_snippets(history_snippets or [], self.memory.context_chars * 2)

    try:
        triage_result = await self.llm_client.triage_email(subject, body, history_snippets=history_snippets)
        try:
            reflected_result = await self.llm_client.reflect_triage(triage_result, subject, body)
        except LLMError as exc:
            workflow_state.metadata["triage_reflection"] = {"status": "failed", "error_type": type(exc).__name__}
            workflow_state.record_history(
                "triage_reflection", "failed", f"Initial triage retained ({type(exc).__name__})"
            )
            logger.warning(
                "case=%s stage=triage_reflection status=failed error_type=%s",
                case_id,
                type(exc).__name__,
            )
        else:
            triage_result = apply_triage_reflection(triage_result, reflected_result)
            workflow_state.metadata["triage_reflection"] = {
                "status": "completed",
                **reflected_result.model_dump(mode="json"),
            }
            workflow_state.record_history(
                "triage_reflection", "completed", f"Reflection outcome: {reflected_result.outcome}"
            )
            logger.info("case=%s stage=triage_reflection status=completed", case_id)
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


@_trace_step("workflow.check_clarification")
async def _check_clarification_node(self: WorkflowContext, state: GraphState) -> GraphState:
    """Check if clarification is needed."""
    # This node just passes through, the decision is made in _should_clarify
    return state


@_trace_step("workflow.faq_gate")
async def _faq_gate_node(self: WorkflowContext, state: GraphState) -> GraphState:
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
        workflow_state.record_history("faq_gate", "failed", f"FAQ search failed: {faq_results.get('error', 'unknown')}")
        logger.warning(
            "case=%s stage=faq_gate status=failed error=%s",
            case_id,
            faq_results.get("error", "unknown"),
        )

    return state
