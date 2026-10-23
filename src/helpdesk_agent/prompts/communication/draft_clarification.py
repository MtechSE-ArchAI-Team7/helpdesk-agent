from __future__ import annotations

from helpdesk_agent.domain.models.triage import TriageResult


def draft_clarification(
    triage: TriageResult,
    raw_text: str,
) -> str:
    """
    Draft a clarification email based on triage results.

    Args:
        triage: The triage result with clarifying questions
        raw_text: Original email text

    Returns:
        Drafted clarification email body
    """
    if not triage.clarifying_questions:
        return """Thank you for contacting support.

We need more information to assist you with your request.

Could you please provide more details about the issue you're experiencing?

Best regards,
Helpdesk Team"""

    questions = "\n".join(f"- {q}" for q in triage.clarifying_questions)
    return f"""Thank you for contacting support.

We're reviewing your request and need some additional information:

{questions}

Please reply to this email with the requested details.

Best regards,
Helpdesk Team"""
