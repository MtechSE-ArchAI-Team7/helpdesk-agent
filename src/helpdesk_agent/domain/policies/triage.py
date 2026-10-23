from __future__ import annotations

from typing import Any

from helpdesk_agent.domain.models.browser import PlaywrightAssessment
from helpdesk_agent.domain.models.triage import TriageResult

CLARIFICATION_COMMENT_MARKER = "pending clarifications"
DIAGNOSIS_AGENT_NAME = "diagnosis-agent"


def clarification_rounds(ticket: dict[str, Any]) -> int:
    return sum(
        CLARIFICATION_COMMENT_MARKER in (comment.get("body", "") if isinstance(comment, dict) else str(comment)).lower()
        for comment in ticket.get("comments", []) or []
    )


def needs_clarification(
    triage: TriageResult,
    assessment: PlaywrightAssessment | None,
    confidence_threshold: float,
) -> bool:
    return (
        triage.confidence < confidence_threshold
        or bool(triage.missing_info)
        or bool(assessment and assessment.decision == "needs_info")
    )


def relevant_faq(result: dict[str, Any], relevance_threshold: float) -> bool:
    score: float = result.get("score", 0)
    return score >= relevance_threshold
