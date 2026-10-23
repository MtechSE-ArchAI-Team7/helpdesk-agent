from __future__ import annotations

import logging

from helpdesk_agent.domain.policies.triage import (
    clarification_rounds,
    needs_clarification,
    relevant_faq,
)
from helpdesk_agent.observability.events import _record_gate_decision
from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


def _has_existing_ticket(self: WorkflowContext, state: GraphState) -> str:
    """Route based on whether an existing ticket was loaded."""
    return "existing" if state.get("existing_ticket") else "new"


def _ticket_created(self: WorkflowContext, state: GraphState) -> str:
    """Stop the run if ticket creation failed — without a real ticket ID,
    every downstream Jira call and the acknowledgment subject are corrupt."""
    return "created" if state.get("ticket_id") else "failed"


def _clarification_rounds(self: WorkflowContext, state: GraphState) -> int:
    return clarification_rounds(state.get("existing_ticket") or {})


def _should_clarify(self: WorkflowContext, state: GraphState) -> str:
    """Determine if clarification is needed."""
    case_id = state["workflow_state"].case_id
    triage_result = state.get("triage_result")
    if not triage_result:
        _record_gate_decision(
            state,
            stage="clarification_gate",
            decision="faq_gate",
            reason_code="no_triage_result",
        )
        logger.info("case=%s gate=clarification decision=faq_gate reason=no_triage_result", case_id)
        return "faq_gate"

    threshold = self.triage_config.confidence_threshold
    assessment = state.get("playwright_assessment")
    clarification_needed = needs_clarification(triage_result, assessment, threshold)
    if not clarification_needed:
        if assessment and assessment.decision == "eligible":
            _record_gate_decision(
                state,
                stage="clarification_gate",
                decision="playwright",
                reason_code="browser_check_eligible",
                details={"confidence": triage_result.confidence, "confidence_threshold": threshold},
            )
            logger.info("case=%s gate=clarification decision=playwright reason=eligible", case_id)
            return "playwright"
        _record_gate_decision(
            state,
            stage="clarification_gate",
            decision="faq_gate",
            reason_code="confidence_and_information_sufficient",
            details={
                "confidence": triage_result.confidence,
                "confidence_threshold": threshold,
                "missing_info_count": len(triage_result.missing_info),
            },
        )
        logger.info(
            "case=%s gate=clarification decision=faq_gate reason=confident confidence=%.2f threshold=%.2f",
            case_id,
            triage_result.confidence,
            threshold,
        )
        return "faq_gate"

    rounds = self._clarification_rounds(state)
    max_rounds = self.triage_config.max_clarification_rounds
    if rounds >= max_rounds:
        if assessment and assessment.decision == "eligible" and assessment.target_url:
            _record_gate_decision(
                state,
                stage="clarification_gate",
                decision="playwright",
                reason_code="clarification_round_limit_reached_browser_check_eligible",
                details={
                    "clarification_round": rounds,
                    "max_clarification_rounds": max_rounds,
                },
            )
            logger.info(
                "case=%s gate=clarification decision=playwright reason=max_rounds rounds=%d max=%d",
                case_id,
                rounds,
                max_rounds,
            )
            return "playwright"
        _record_gate_decision(
            state,
            stage="clarification_gate",
            decision="diagnose",
            reason_code="clarification_round_limit_reached",
            details={
                "clarification_round": rounds,
                "max_clarification_rounds": max_rounds,
            },
        )
        logger.info(
            "case=%s gate=clarification decision=diagnose reason=max_rounds rounds=%d max=%d",
            case_id,
            rounds,
            max_rounds,
        )
        return "diagnose"

    _record_gate_decision(
        state,
        stage="clarification_gate",
        decision="clarify",
        reason_code="confidence_low_or_information_missing",
        details={
            "confidence": triage_result.confidence,
            "confidence_threshold": threshold,
            "missing_info_count": len(triage_result.missing_info),
            "clarification_round": rounds,
            "max_clarification_rounds": max_rounds,
        },
    )
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


def _should_resolve_faq(self: WorkflowContext, state: GraphState) -> str:
    """Determine if FAQ solution should be used."""
    case_id = state["workflow_state"].case_id
    faq_results = state.get("faq_results") or {}
    if faq_results.get("status") != "success":
        _record_gate_decision(
            state,
            stage="faq_gate",
            decision="diagnose",
            reason_code="faq_search_failed",
        )
        logger.info("case=%s gate=faq decision=diagnose reason=faq_search_failed", case_id)
        return "diagnose"

    results = faq_results.get("results", [])
    if not results:
        _record_gate_decision(
            state,
            stage="faq_gate",
            decision="diagnose",
            reason_code="no_faq_matches",
            details={"faq_result_count": 0},
        )
        logger.info("case=%s gate=faq decision=diagnose reason=no_faq_matches", case_id)
        return "diagnose"

    # Check relevance threshold
    threshold = self.triage_config.faq_relevance_threshold
    top_result = results[0]
    if relevant_faq(top_result, threshold):
        _record_gate_decision(
            state,
            stage="faq_gate",
            decision="resolve_faq",
            reason_code="top_match_met_relevance_threshold",
            details={
                "faq_score": top_result.get("score", 0),
                "faq_threshold": threshold,
                "faq_result_count": len(results),
            },
        )
        logger.info(
            "case=%s gate=faq decision=resolve_faq reason=relevant_match score=%.3f threshold=%.2f",
            case_id,
            top_result.get("score", 0),
            threshold,
        )
        return "resolve_faq"
    _record_gate_decision(
        state,
        stage="faq_gate",
        decision="diagnose",
        reason_code="top_match_below_relevance_threshold",
        details={
            "faq_score": top_result.get("score", 0),
            "faq_threshold": threshold,
            "faq_result_count": len(results),
        },
    )
    logger.info(
        "case=%s gate=faq decision=diagnose reason=below_threshold score=%.3f threshold=%.2f",
        case_id,
        top_result.get("score", 0),
        threshold,
    )
    return "diagnose"
