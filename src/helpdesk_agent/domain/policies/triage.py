from __future__ import annotations

from typing import Any

from helpdesk_agent.domain.models.browser import PlaywrightAssessment
from helpdesk_agent.domain.models.triage import TriageReflection, TriageResult

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
    *,
    reflection_uncertain: bool = False,
) -> bool:
    return (
        reflection_uncertain
        or triage.confidence < confidence_threshold
        or bool(triage.missing_info)
        or bool(assessment and assessment.decision == "needs_info")
    )


def apply_triage_reflection(initial: TriageResult, reflection: TriageReflection) -> TriageResult:
    if reflection.outcome == "revise" and reflection.revised_triage is not None:
        return initial.model_copy(update=reflection.revised_triage.model_dump())
    if reflection.outcome == "uncertain":
        return initial.model_copy(
            update={
                "missing_info": list(dict.fromkeys([*initial.missing_info, *reflection.missing_info])),
                "clarifying_questions": list(
                    dict.fromkeys([*initial.clarifying_questions, *reflection.clarifying_questions])
                ),
            }
        )
    return initial


def relevant_faq(result: dict[str, Any], relevance_threshold: float) -> bool:
    score: float = result.get("score", 0)
    return score >= relevance_threshold
