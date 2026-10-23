import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from helpdesk_agent.domain.exceptions import LLMError
from helpdesk_agent.domain.models.triage import ReflectionRevision, TriageReflection, TriageResult
from helpdesk_agent.domain.policies.triage import apply_triage_reflection, needs_clarification
from helpdesk_agent.reasoning.client import HelpdeskReasoner
from helpdesk_agent.reasoning.guardrails import GuardrailsValidator

pytestmark = pytest.mark.unit


def initial():
    return TriageResult(
        service="identity",
        issue_type="access",
        priority="high",
        confidence=0.9,
        summary="Account locked",
        source="chat",
        sender="requester",
        raw_text="My account is locked",
    )


def revised():
    return ReflectionRevision(
        service="identity",
        issue_type="access",
        priority="medium",
        confidence=0.8,
        summary="One account is locked",
    )


def uncertain():
    return TriageReflection(
        outcome="uncertain",
        assessment_summary="Impact is unspecified",
        findings=["The number of affected users is not reported"],
        missing_info=["affected users"],
        clarifying_questions=["Who is affected?"],
    )


def test_accept_preserves_all_initial_fields():
    result = initial()
    assessment = TriageReflection(outcome="accept", assessment_summary="Supported by the report")
    assert apply_triage_reflection(result, assessment).model_dump() == result.model_dump()


def test_revise_changes_validated_fields_and_preserves_provenance():
    result = initial()
    assessment = TriageReflection(outcome="revise", assessment_summary="Impact is limited", revised_triage=revised())
    applied = apply_triage_reflection(result, assessment)
    assert applied.priority == "medium"
    assert applied.summary == "One account is locked"
    assert (applied.source, applied.sender, applied.raw_text) == (result.source, result.sender, result.raw_text)
    assert result.priority == "high"


def test_uncertainty_preserves_classification_and_deduplicates_questions():
    result = initial().model_copy(
        update={"missing_info": ["affected users"], "clarifying_questions": ["Who is affected?"]}
    )
    applied = apply_triage_reflection(result, uncertain())
    assert applied.model_dump() == result.model_dump()
    assert needs_clarification(initial(), None, 0.6, reflection_uncertain=True)
    assert not needs_clarification(initial(), None, 0.6)


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": "unknown"},
        {"assessment_summary": " "},
        {"findings": [" "]},
        {"unexpected": "not allowed"},
        {"outcome": "revise"},
        {"revised_triage": revised().model_dump()},
        {"missing_info": ["affected users"]},
        {"outcome": "uncertain"},
        {"outcome": "uncertain", "missing_info": ["affected users"], "clarifying_questions": [" "]},
        {
            "outcome": "revise",
            "revised_triage": {**revised().model_dump(), "confidence": 2},
        },
        {
            "outcome": "revise",
            "revised_triage": {**revised().model_dump(), "service": "not-a-service"},
        },
        {
            "outcome": "revise",
            "revised_triage": {**revised().model_dump(), "summary": " "},
        },
        {
            "outcome": "revise",
            "revised_triage": {**revised().model_dump(), "extra_field": "not allowed"},
        },
    ],
)
def test_invalid_reflection_payload_is_rejected(overrides):
    with pytest.raises(ValidationError):
        TriageReflection.model_validate({"outcome": "accept", "assessment_summary": "Supported", **overrides})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assessment",
    [
        TriageReflection(outcome="accept", assessment_summary="Supported"),
        TriageReflection(outcome="revise", assessment_summary="Impact is limited", revised_triage=revised()),
        uncertain(),
    ],
)
async def test_actual_guardrails_accepts_reflection_schema(assessment):
    await GuardrailsValidator().validate_output(assessment.model_dump_json(), TriageReflection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"outcome": "revise", "assessment_summary": "Unsupported revision"},
        {"outcome": "uncertain", "assessment_summary": "Missing actionable details"},
        {"outcome": "accept", "assessment_summary": " "},
    ],
)
async def test_reasoner_wraps_invalid_outcomes_as_explicit_errors(payload):
    guardrails = MagicMock()
    guardrails.validate_input = AsyncMock()
    reasoner = HelpdeskReasoner(MagicMock(), guardrails)
    reasoner._complete_json = AsyncMock(return_value=payload)
    with pytest.raises(LLMError, match="reflection failed"):
        await reasoner.reflect_triage(initial(), "Account locked", "My account is locked")


def test_metadata_reflection_is_json_serializable():
    from helpdesk_agent.domain.models.workflow import WorkflowState

    state = WorkflowState(case_id="HELP-1", summary="Account locked")
    state.metadata["triage_reflection"] = {"status": "completed", **uncertain().model_dump(mode="json")}
    assert json.loads(json.dumps(state.to_dict()))["metadata"]["triage_reflection"]["outcome"] == "uncertain"
