from __future__ import annotations

from evaluations.support.schemas import (
    AssessmentCase,
    EvidenceCase,
    PlanningCase,
    ReactCase,
    TriageCase,
)
from helpdesk_agent.api.factory import CLARIFICATION_COMMENT_MARKER, GraphState, HelpdeskWorkflow
from helpdesk_agent.config.settings import TriageConfig
from helpdesk_agent.domain.models.triage import TriageResult
from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.infrastructure.llm.client import LLMTriageClient


def evaluate_route(client: LLMTriageClient, case: TriageCase, result: TriageResult) -> str:
    fixture = case.routing
    if fixture is None:
        raise ValueError("Routing evaluation requires a fixture")
    workflow = HelpdeskWorkflow(
        llm_client=client,
        triage_config=TriageConfig(
            confidence_threshold=fixture.confidence_threshold,
            faq_relevance_threshold=fixture.faq_relevance_threshold,
            max_clarification_rounds=fixture.max_clarification_rounds,
        ),
    )
    state: GraphState = {
        "workflow_state": WorkflowState(case_id=case.id, summary=result.summary),
        "initial_input": {"subject": case.subject, "body": case.body},
        "triage_result": result,
        "ticket_id": "HELP-1",
        "existing_ticket": {"comments": [CLARIFICATION_COMMENT_MARKER] * fixture.clarification_rounds},
        "faq_results": {
            "status": fixture.faq_status,
            "results": [] if fixture.faq_score is None else [{"score": fixture.faq_score}],
        },
        "playwright_assessment": fixture.browser_assessment,
        "playwright_validation": None,
    }
    route = workflow._should_clarify(state)
    return workflow._should_resolve_faq(state) if route == "faq_gate" else route


async def run_case(
    client: LLMTriageClient,
    case: TriageCase | AssessmentCase | EvidenceCase | PlanningCase | ReactCase,
) -> dict[str, object]:
    if isinstance(case, TriageCase):
        result = await client.triage_email(case.subject, case.body, history_snippets=case.history_snippets)
        actual = result.model_dump()
        if case.routing:
            actual["route"] = evaluate_route(client, case, result)
        return actual
    if isinstance(case, AssessmentCase):
        assessment = await client.assess_playwright_validation(case.subject, case.body, case.triage_summary)
        return assessment.model_dump()
    if isinstance(case, EvidenceCase):
        evaluation = await client.evaluate_playwright_validation(case.expected_error, case.evidence)
        return evaluation.model_dump()
    if isinstance(case, PlanningCase):
        plan = await client.plan_browser(case.target_url, case.expected_error, case.summary)
        return plan.model_dump()
    action = await client.react_browser(
        case.target_url,
        case.expected_error,
        case.summary,
        [str(tool) for tool in case.inspection_order],
        case.observations,
        case.round_number,
    )
    return action.model_dump()
