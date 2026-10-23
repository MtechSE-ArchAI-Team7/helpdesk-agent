from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from deepeval.test_case import LLMTestCase

from evaluations.support.datasets import load_cases, select_cases
from evaluations.support.metrics import HelpdeskBehaviorMetric, TriageLabelMetric, assert_evaluation
from evaluations.support.runner import evaluate_route, run_case
from evaluations.support.schemas import (
    AREA_COUNTS,
    AssessmentCase,
    AssessmentExpected,
    EvidenceCase,
    Labels,
    RoutingFixture,
    TextCheck,
    TriageCase,
    TriageExpected,
)
from helpdesk_agent.config.settings import LLMConfig
from helpdesk_agent.domain.models.browser import PlaywrightAssessment, PlaywrightEvaluation
from helpdesk_agent.domain.models.triage import TriageResult
from helpdesk_agent.infrastructure.llm.client import LLMError, LLMTriageClient
from helpdesk_agent.reasoning.guardrails import get_guardrails_validator

SUITE = os.environ.get("HELPDESK_AGENT_EVAL_SUITE", "testing")
CASES = select_cases(SUITE)
LABELS = Labels(service="identity", issue_type="access", priority="medium")


@pytest.mark.unit
@pytest.mark.parametrize("priority", ["highest", "high", "medium", "low", "lowest"])
def test_evaluation_labels_accept_five_level_priority_policy(priority: str) -> None:
    from helpdesk_agent.domain.models.triage import TriageLLMResponse

    labels = Labels.model_validate({**LABELS.model_dump(), "priority": priority})
    result = TriageLLMResponse.model_validate(
        {**labels.model_dump(), "confidence": 0.9, "summary": "Reported support issue"}
    )
    assert result.priority == labels.priority == priority


@pytest.mark.functional
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
async def test_live_triage(case: TriageCase | AssessmentCase | EvidenceCase) -> None:
    config = LLMConfig.from_env()
    if not config.api_key or config.api_key == "test-api-key":
        pytest.fail("Live evaluation requires HELPDESK_AGENT_LLM_API_KEY, not a dummy test key")
    started = perf_counter()
    client = LLMTriageClient(config)
    try:
        actual = await run_case(client, case)
    finally:
        await client.client.close()
    execution_duration_seconds = perf_counter() - started
    assert_evaluation(
        LLMTestCase(
            input=case.model_dump_json(exclude={"expected", "checks"}),
            actual_output=json.dumps(actual),
            expected_output=case.expected.model_dump_json(exclude_unset=True),
            name=case.id,
            metadata={
                "suite": SUITE,
                "case_id": case.id,
                "operation": case.operation,
                "area": case.area,
                "model": config.model,
                "execution_duration_seconds": execution_duration_seconds,
            },
        ),
        HelpdeskBehaviorMetric(case),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "actual,score",
    [
        ({"service": "identity", "issue_type": "access", "priority": "medium"}, 1.0),
        ({"service": "identity", "issue_type": "access", "priority": "low"}, 2 / 3),
        ({}, 0.0),
    ],
)
def test_metric_label_scores(actual: dict[str, str], score: float) -> None:
    metric = TriageLabelMetric()
    case = LLMTestCase(input="test", actual_output=json.dumps(actual), expected_output=LABELS.model_dump_json())
    assert metric.measure(case) == pytest.approx(score)
    assert metric.is_successful() == (score == 1.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metric_async_parity() -> None:
    labels = LABELS.model_dump_json()
    case = LLMTestCase(input="test", actual_output=labels, expected_output=labels)
    metric = TriageLabelMetric()
    assert await metric.a_measure(case) == metric.measure(case) == 1.0


@pytest.mark.unit
def test_deepeval_assertion_pass_and_failure() -> None:
    labels = LABELS.model_dump_json()
    assert_evaluation(
        LLMTestCase(input="offline", actual_output=labels, expected_output=labels),
        TriageLabelMetric(),
    )
    with pytest.raises(AssertionError):
        assert_evaluation(
            LLMTestCase(input="offline", actual_output="{}", expected_output=labels),
            TriageLabelMetric(),
        )


@pytest.mark.unit
@pytest.mark.parametrize("actual", ["invalid JSON", "[]"])
def test_metric_rejects_invalid_output(actual: str) -> None:
    metric = TriageLabelMetric()
    with pytest.raises(ValueError):
        metric.measure(LLMTestCase(input="test", actual_output=actual, expected_output=LABELS.model_dump_json()))
    assert not metric.is_successful()


@pytest.mark.unit
@pytest.mark.parametrize(
    "data",
    [
        {"cases": []},
        {"cases": [{"id": "incomplete"}]},
        {"cases": [CASES[0].model_dump(exclude_unset=True)] * 2},
    ],
)
def test_dataset_rejects_invalid_cases(tmp_path: Path, data: dict[str, object]) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


@pytest.mark.unit
def test_suite_inheritance_and_coverage() -> None:
    testing = select_cases("testing")
    evaluation = select_cases("evaluation")
    assert len(testing) == 18
    assert len(evaluation) == 60
    assert evaluation[: len(testing)] == testing
    for cases in (testing, evaluation):
        assert {case.operation for case in cases} == {"triage", "browser_assessment", "browser_evidence"}
        assert any(isinstance(case, TriageCase) and case.routing for case in cases)
    assert Counter(case.area for case in evaluation) == AREA_COUNTS
    assert all(case.area is not None for case in testing)


@pytest.mark.unit
@pytest.mark.parametrize("area", [None, "unsupported"])
def test_dataset_requires_known_area(tmp_path: Path, area: str | None) -> None:
    row = select_cases("testing")[0].model_dump()
    row["area"] = area
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [row]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


@pytest.mark.unit
def test_all_scenarios_reference_ecommerce_demo() -> None:
    for case in select_cases("evaluation"):
        text = case.evidence if isinstance(case, EvidenceCase) else f"{case.subject}\n{case.body}"
        assert "Buggy E-Commerce Demo" in text, case.id
        if isinstance(case, TriageCase) and case.expected.service is not None:
            assert case.expected.service in {"platform", "identity"}, case.id


@pytest.mark.unit
@pytest.mark.parametrize("suite", ["", "invalid", "../testing"])
def test_unknown_suite_fails(suite: str) -> None:
    with pytest.raises(ValueError, match="Unknown evaluation suite"):
        select_cases(suite)


@pytest.mark.unit
def test_dataset_inheritance_rejects_duplicate_ids(tmp_path: Path) -> None:
    row = select_cases("testing")[0].model_dump(exclude_unset=True)
    (tmp_path / "triage_testing.json").write_text(json.dumps({"cases": [row]}), encoding="utf-8")
    path = tmp_path / "triage_evaluation.json"
    path.write_text(json.dumps({"extends": "testing", "cases": [row]}), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_cases(path)


@pytest.mark.unit
def test_testing_cannot_extend_itself(tmp_path: Path) -> None:
    path = tmp_path / "triage_testing.json"
    path.write_text(
        json.dumps({"extends": "testing", "cases": [select_cases("testing")[0].model_dump(exclude_unset=True)]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Only the evaluation"):
        load_cases(path)


@pytest.mark.unit
@pytest.mark.parametrize(
    "change",
    [
        {"checks": [{"field": "reason", "pattern": ".+"}]},
        {"checks": [{"field": "summary", "pattern": "["}]},
        {"operation": "invalid"},
        {"routing": {}},
        {"expected": {}},
        {"expected": {"service": "jira", "priority": None}},
        {"expected": {"service": "jira", "issue_type": "access", "priority": "medium", "route": "clarify"}},
    ],
)
def test_dataset_rejects_invalid_contracts(tmp_path: Path, change: dict[str, object]) -> None:
    row = select_cases("testing")[0].model_dump(exclude_unset=True)
    row.update(change)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [row]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


def sample_triage(**overrides: object) -> TriageResult:
    return TriageResult.model_validate(
        {
            **LABELS.model_dump(),
            "confidence": 0.9,
            "summary": "One Buggy E-Commerce Demo customer cannot sign in.",
            **overrides,
        }
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "changes,success",
    [
        ({}, True),
        ({"priority": "low"}, False),
        ({"summary": "Please send your password"}, False),
        ({"clarifying_questions": ["What is your password?"]}, False),
        ({"summary": "Do not send your password."}, True),
        ({"raw_text": "Please send your password"}, True),
    ],
)
def test_behavior_metric_labels_and_secret_requests(changes: dict[str, object], success: bool) -> None:
    case = select_cases("testing")[0].model_copy(update={"expected": TriageExpected(**LABELS.model_dump())})
    metric = HelpdeskBehaviorMetric(case)
    actual = sample_triage(**changes).model_dump_json()
    assert metric.measure(LLMTestCase(input="offline", actual_output=actual)) == float(success)
    assert metric.is_successful() == success


@pytest.mark.unit
@pytest.mark.parametrize(
    "summary,success",
    [
        ("Jira account is locked; the cause is not established.", True),
        ("The database caused the Jira outage.", False),
    ],
)
def test_behavior_metric_text_checks(summary: str, success: bool) -> None:
    case = TriageCase(
        id="text",
        subject="Jira",
        body="Account locked",
        expected=TriageExpected(**LABELS.model_dump()),
        checks=[
            TextCheck(field="summary", pattern="account"),
            TextCheck(field="summary", pattern="database", present=False),
        ],
    )
    metric = HelpdeskBehaviorMetric(case)
    assert metric.measure(
        LLMTestCase(input="offline", actual_output=sample_triage(summary=summary).model_dump_json())
    ) == float(success)


@pytest.mark.unit
@pytest.mark.parametrize(
    "url,error,success",
    [
        ("https://shop.example.test/", "Blank product catalog", True),
        ("https://invented.example.com/health", "Service unavailable", False),
        ("https://shop.example.test/", "", False),
    ],
)
def test_assessment_metric_checks_url_and_error(url: str, error: str, success: bool) -> None:
    case = next(
        case
        for case in select_cases("testing")
        if isinstance(case, AssessmentCase) and case.expected.decision == "eligible"
    )
    metric = HelpdeskBehaviorMetric(case)
    actual = PlaywrightAssessment(
        decision="eligible",
        reason="Public page",
        target_url=url,
        expected_error=error,
    )
    assert metric.measure(LLMTestCase(input="offline", actual_output=actual.model_dump_json())) == float(success)


@pytest.mark.unit
@pytest.mark.parametrize("actual", ["invalid", "[]", "{}", '{"status":"invalid","summary":"Error"}'])
def test_behavior_metric_rejects_invalid_output(actual: str) -> None:
    case = next(case for case in select_cases("testing") if isinstance(case, EvidenceCase))
    metric = HelpdeskBehaviorMetric(case)
    with pytest.raises(ValueError):
        metric.measure(LLMTestCase(input="offline", actual_output=actual))
    assert not metric.is_successful()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_behavior_metric_async_parity_and_deepeval_assertion() -> None:
    case = select_cases("testing")[0].model_copy(update={"expected": TriageExpected(**LABELS.model_dump())})
    test_case = LLMTestCase(input="offline", actual_output=sample_triage().model_dump_json())
    metric = HelpdeskBehaviorMetric(case)
    assert await metric.a_measure(test_case) == metric.measure(test_case) == 1.0
    assert_evaluation(test_case, HelpdeskBehaviorMetric(case))
    with pytest.raises(AssertionError):
        assert_evaluation(
            LLMTestCase(input="offline", actual_output=sample_triage(priority="low").model_dump_json()),
            HelpdeskBehaviorMetric(case),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "confidence,missing,rounds,browser,score,status,expected",
    [
        (0.59, [], 0, None, 0.9, "success", "clarify"),
        (0.6, [], 0, None, 0.7, "success", "resolve_faq"),
        (0.9, ["error"], 0, None, 0.9, "success", "clarify"),
        (0.9, [], 0, "needs_info", 0.9, "success", "clarify"),
        (0.59, [], 2, None, None, "success", "diagnose"),
        (0.59, [], 2, "eligible", None, "success", "playwright"),
        (0.9, [], 0, None, 0.69, "success", "diagnose"),
        (0.9, [], 0, None, None, "success", "diagnose"),
        (0.9, [], 0, None, 0.95, "failed", "diagnose"),
    ],
)
def test_live_result_routing_uses_production_gates(
    confidence: float,
    missing: list[str],
    rounds: int,
    browser: Literal["needs_info", "eligible"] | None,
    score: float | None,
    status: Literal["success", "failed"],
    expected: Literal["clarify", "diagnose", "playwright", "resolve_faq"],
) -> None:
    assessment = (
        None
        if browser is None
        else PlaywrightAssessment(
            decision=browser,
            reason="Fixture",
            target_url="https://status.example.com/health",
        )
    )
    case = TriageCase(
        id="route",
        subject="Jira",
        body="Account locked",
        expected=TriageExpected(**LABELS.model_dump(), route=expected),
        routing=RoutingFixture(
            clarification_rounds=rounds,
            browser_assessment=assessment,
            faq_score=score,
            faq_status=status,
        ),
    )
    client = MagicMock(spec=LLMTriageClient)
    assert evaluate_route(client, case, sample_triage(confidence=confidence, missing_info=missing)) == expected
    client.triage_email.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["triage", "browser_assessment", "browser_evidence"])
async def test_runner_dispatches_one_model_call(operation: str) -> None:
    case = next(case for case in select_cases("testing") if case.operation == operation)
    client = MagicMock(spec=LLMTriageClient)
    client.triage_email = AsyncMock(return_value=sample_triage())
    client.assess_playwright_validation = AsyncMock(
        return_value=PlaywrightAssessment(decision="needs_info", reason="URL missing"),
    )
    client.evaluate_playwright_validation = AsyncMock(
        return_value=PlaywrightEvaluation(status="inconclusive", summary="Insufficient evidence"),
    )
    actual = await run_case(client, case)
    assert actual
    if isinstance(case, TriageCase):
        client.triage_email.assert_awaited_once_with(
            case.subject,
            case.body,
            history_snippets=case.history_snippets,
        )
    elif isinstance(case, AssessmentCase):
        client.assess_playwright_validation.assert_awaited_once_with(case.subject, case.body, case.triage_summary)
    else:
        client.evaluate_playwright_validation.assert_awaited_once_with(case.expected_error, case.evidence)
    assert (
        sum(
            method.await_count
            for method in (
                client.triage_email,
                client.assess_playwright_validation,
                client.evaluate_playwright_validation,
            )
        )
        == 1
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_runner_closes_client_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock(spec=LLMTriageClient)
    client.client = MagicMock()
    client.client.close = AsyncMock()
    client.triage_email = AsyncMock(side_effect=LLMError("Synthetic model failure"))
    monkeypatch.setenv("HELPDESK_AGENT_LLM_API_KEY", "synthetic-evaluation-key")
    monkeypatch.setattr(f"{__name__}.LLMTriageClient", lambda config: client)
    with pytest.raises(LLMError, match="Synthetic model failure"):
        await test_live_triage(select_cases("testing")[0])
    client.client.close.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "test-api-key"])
async def test_live_runner_rejects_missing_or_dummy_credentials(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    monkeypatch.setenv("HELPDESK_AGENT_LLM_API_KEY", key)
    with pytest.raises(pytest.fail.Exception, match="Live evaluation requires"):
        await test_live_triage(select_cases("testing")[0])


@pytest.mark.unit
def test_assessment_url_can_be_in_subject() -> None:
    case = AssessmentCase(
        id="subject-url",
        operation="browser_assessment",
        subject="https://status.example.com/health",
        body="The public page shows an error on load.",
        triage_summary="Public page error",
        expected=AssessmentExpected(decision="eligible", target_url="https://status.example.com/health"),
    )
    actual = PlaywrightAssessment(
        decision="eligible",
        reason="Public page",
        target_url=case.expected.target_url,
        expected_error="Page error",
    )
    metric = HelpdeskBehaviorMetric(case)
    assert metric.measure(LLMTestCase(input="offline", actual_output=actual.model_dump_json())) == 1.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "route,success",
    [("clarify", True), ("diagnose", False)],
)
def test_behavior_metric_checks_actual_route_and_explains_mismatch(route: str, success: bool) -> None:
    case = TriageCase(
        id="route-metric",
        subject="Help",
        body="Something isn't working.",
        routing=RoutingFixture(confidence_threshold=0.6, clarification_rounds=1),
        expected=TriageExpected(route="clarify"),
    )
    actual = sample_triage().model_dump()
    actual["route"] = route
    actual["confidence"] = 0.4
    actual["missing_info"] = ["affected service"]
    actual["clarifying_questions"] = ["Which service is affected?"]
    metric = HelpdeskBehaviorMetric(case)
    assert metric.measure(LLMTestCase(input="offline", actual_output=json.dumps(actual))) == float(success)
    if not success:
        assert metric.reason is not None
        assert '"confidence": 0.4' in metric.reason
        assert '"confidence_threshold": 0.6' in metric.reason
        assert '"missing_info": ["affected service"]' in metric.reason
        assert '"clarifying_questions": ["Which service is affected?"]' in metric.reason
        assert '"clarification_rounds": 1' in metric.reason


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case", select_cases("evaluation"), ids=lambda case: case.id)
async def test_all_live_case_inputs_pass_production_guardrails(
    case: TriageCase | AssessmentCase | EvidenceCase,
) -> None:
    if isinstance(case, TriageCase):
        values = {
            "subject": case.subject,
            "body": case.body,
            **{f"history_{index}": snippet for index, snippet in enumerate(case.history_snippets)},
        }
    elif isinstance(case, AssessmentCase):
        values = {"subject": case.subject, "body": case.body, "triage_summary": case.triage_summary}
    else:
        values = {"expected_error": case.expected_error, "evidence": case.evidence}
    await get_guardrails_validator().validate_input(values)
