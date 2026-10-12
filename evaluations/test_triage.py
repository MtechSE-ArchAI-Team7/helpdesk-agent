from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from deepeval.evaluate import assert_test
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase
from pydantic import BaseModel, ConfigDict, Field, model_validator

from helpdesk_agent.config import LLMConfig, TriageConfig
from helpdesk_agent.core.workflow import CLARIFICATION_COMMENT_MARKER, GraphState, HelpdeskWorkflow
from helpdesk_agent.models import WorkflowState
from helpdesk_agent.modules.playwright import PlaywrightAssessment, PlaywrightEvaluation, PlaywrightValidationFlow
from helpdesk_agent.modules.triage.guardrails import get_guardrails_validator
from helpdesk_agent.modules.triage.llm import LLMError, LLMTriageClient
from helpdesk_agent.modules.triage.schema import TriageLLMResponse, TriageResult

DATASET_DIR = Path(__file__).parent
SUITES = ("testing", "evaluation")
Service = Literal["jira", "slack", "email", "identity", "platform", "prometheus", "unknown"]
IssueType = Literal["access", "incident", "performance", "deployment", "general"]
Priority = Literal["critical", "high", "medium", "low"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Labels(StrictModel):
    service: Service
    issue_type: IssueType
    priority: Priority


class TextCheck(StrictModel):
    field: Literal["summary", "clarifying_questions", "missing_info", "reason", "expected_error"]
    pattern: str = Field(min_length=1)
    present: bool = True

    @model_validator(mode="after")
    def validate_pattern(self) -> TextCheck:
        try:
            re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid text-check pattern: {exc}") from exc
        return self


class RoutingFixture(StrictModel):
    clarification_rounds: int = Field(default=0, ge=0)
    max_clarification_rounds: int = Field(default=2, ge=0)
    confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    faq_relevance_threshold: float = Field(default=0.7, ge=0, le=1)
    faq_score: float | None = Field(default=None, ge=0, le=1)
    faq_status: Literal["success", "failed"] = "success"
    browser_assessment: PlaywrightAssessment | None = None


class TriageExpected(StrictModel):
    service: Service | None = None
    issue_type: IssueType | None = None
    priority: Priority | None = None
    route: Literal["clarify", "diagnose", "playwright", "resolve_faq"] | None = None

    @model_validator(mode="after")
    def validate_expectations(self) -> TriageExpected:
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("At least one triage expectation is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Omit unconstrained triage fields instead of setting them to null")
        return self


class AssessmentExpected(StrictModel):
    decision: Literal["eligible", "needs_info", "not_applicable"]
    target_url: str | None = None

    @model_validator(mode="after")
    def validate_target(self) -> AssessmentExpected:
        if self.decision == "eligible" and not self.target_url:
            raise ValueError("Eligible assessment expectations must have a nonempty target URL")
        return self


class EvidenceExpected(StrictModel):
    status: Literal["reproduced", "not_reproduced", "inconclusive"]


class CaseBase(StrictModel):
    id: str = Field(min_length=1)
    checks: list[TextCheck] = Field(default_factory=list)


class TriageCase(CaseBase):
    operation: Literal["triage"] = "triage"
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    history_snippets: list[str] = Field(default_factory=list)
    routing: RoutingFixture | None = None
    expected: TriageExpected

    @model_validator(mode="after")
    def validate_routing(self) -> TriageCase:
        if (self.expected.route is not None) != (self.routing is not None):
            raise ValueError("A routing fixture and expected route must be supplied together")
        return self


class AssessmentCase(CaseBase):
    operation: Literal["browser_assessment"]
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    triage_summary: str = Field(min_length=1)
    expected: AssessmentExpected


class EvidenceCase(CaseBase):
    operation: Literal["browser_evidence"]
    expected_error: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    expected: EvidenceExpected


EvaluationCase = Annotated[TriageCase | AssessmentCase | EvidenceCase, Field(discriminator="operation")]


class Dataset(StrictModel):
    extends: Literal["testing"] | None = None
    cases: list[EvaluationCase] = Field(min_length=1)


def load_cases(path: Path) -> list[TriageCase | AssessmentCase | EvidenceCase]:
    dataset = Dataset.model_validate_json(path.read_text(encoding="utf-8"))
    cases = list(dataset.cases)
    if dataset.extends:
        if path.name != "triage_evaluation.json":
            raise ValueError("Only the evaluation dataset can extend the testing dataset")
        cases = load_cases(path.with_name("triage_testing.json")) + cases
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be unique")
    for case in cases:
        fields = {
            "triage": {"summary", "clarifying_questions", "missing_info"},
            "browser_assessment": {"reason", "expected_error", "clarifying_questions", "missing_info"},
            "browser_evidence": {"summary"},
        }[case.operation]
        if any(check.field not in fields for check in case.checks):
            raise ValueError(f"Case {case.id}: text check targets a field absent from {case.operation}")
    return cases


def select_cases(suite: str) -> list[TriageCase | AssessmentCase | EvidenceCase]:
    if suite not in SUITES:
        raise ValueError(f"Unknown evaluation suite {suite!r}; choose testing or evaluation")
    return load_cases(DATASET_DIR / f"triage_{suite}.json")


class TriageLabelMetric(BaseMetric):  # type: ignore[no-untyped-call]
    def __init__(self) -> None:
        self.threshold = 1.0
        self.async_mode = False
        self.strict_mode = True
        self.error = None
        self.score = 0.0
        self.success = False

    def measure(self, test_case: LLMTestCase, **kwargs: object) -> float:
        self.error = None
        self.success = False
        self.score = 0.0
        try:
            if test_case.actual_output is None:
                raise ValueError("Actual output is required")
            actual = json.loads(test_case.actual_output)
            expected = Labels.model_validate_json(test_case.expected_output or "{}").model_dump()
            if not isinstance(actual, dict):
                raise ValueError("Actual labels must be a JSON object")
            mismatches = [field for field, value in expected.items() if actual.get(field) != value]
            self.score = (len(expected) - len(mismatches)) / len(expected)
            self.success = self.score >= self.threshold
            self.reason = "All labels matched" if not mismatches else f"Mismatched labels: {', '.join(mismatches)}"
            return self.score
        except (ValueError, TypeError) as exc:
            self.error = str(exc)
            raise

    async def a_measure(self, test_case: LLMTestCase, **kwargs: object) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return self.error is None and self.success is True

    @property
    def __name__(self) -> str:
        return "Triage Label Accuracy"


class HelpdeskBehaviorMetric(TriageLabelMetric):  # type: ignore[no-untyped-call]
    def __init__(self, case: TriageCase | AssessmentCase | EvidenceCase) -> None:
        super().__init__()
        self.case = case

    def measure(self, test_case: LLMTestCase, **kwargs: object) -> float:
        self.error = None
        self.success = False
        self.score = 0.0
        try:
            if test_case.actual_output is None:
                raise ValueError("Actual output is required")
            actual = json.loads(test_case.actual_output)
            if not isinstance(actual, dict):
                raise ValueError("Actual output must be a JSON object")
            if isinstance(self.case, TriageCase):
                TriageLLMResponse.model_validate(actual)
            elif isinstance(self.case, AssessmentCase):
                PlaywrightAssessment.model_validate(actual)
            else:
                PlaywrightEvaluation.model_validate(actual)
            expected = self.case.expected.model_dump(exclude_unset=True)
            failures = [
                f"{field}: expected {value!r}, got {actual.get(field)!r}"
                for field, value in expected.items()
                if actual.get(field) != value
            ]
            if (
                isinstance(self.case, TriageCase)
                and self.case.routing is not None
                and actual.get("route") != self.case.expected.route
            ):
                routing = self.case.routing
                failures.append(
                    "routing diagnostics: "
                    + json.dumps(
                        {
                            "confidence": actual.get("confidence"),
                            "confidence_threshold": routing.confidence_threshold,
                            "missing_info": actual.get("missing_info"),
                            "clarifying_questions": actual.get("clarifying_questions"),
                            "browser_decision": (
                                routing.browser_assessment.decision if routing.browser_assessment else None
                            ),
                            "clarification_rounds": routing.clarification_rounds,
                            "max_clarification_rounds": routing.max_clarification_rounds,
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                )
            for check in self.case.checks:
                value = actual.get(check.field)
                if not isinstance(value, (str, list)):
                    raise ValueError(f"Missing text-check field: {check.field}")
                text = "\n".join(value) if isinstance(value, list) else value
                matched = re.search(check.pattern, text, re.IGNORECASE) is not None
                if matched != check.present:
                    failures.append(
                        f"{check.field}: pattern {check.pattern!r} must be {'present' if check.present else 'absent'}"
                    )
            # Inspect generated fields only: raw_text intentionally retains the original report.
            generated = "\n".join(
                str(actual.get(field, "")) for field in ("summary", "reason", "expected_error", "clarifying_questions")
            )
            if re.search(
                r"(?<!not )(?<!never )(?<!don't )(?<!avoid )"
                r"\b(?:send|share|provide|give|enter)\s+(?:me\s+|us\s+)?(?:your\s+|the\s+)?"
                r"(?:password|access token|api key)\b|\bwhat is your password\b",
                generated,
                re.IGNORECASE,
            ):
                failures.append("Generated output requests a secret")
            if isinstance(self.case, AssessmentCase):
                report = f"{self.case.subject}\n\n{self.case.body}"
                target = actual.get("target_url")
                if (target is not None or actual.get("decision") == "eligible") and (
                    target not in PlaywrightValidationFlow.reported_urls(report)
                ):
                    failures.append("Target URL was not explicitly supplied in the report")
                if actual.get("decision") == "eligible" and not str(actual.get("expected_error", "")).strip():
                    failures.append("Eligible assessment has no expected error")
            self.score = 0.0 if failures else 1.0
            self.success = not failures
            self.reason = "; ".join(failures) if failures else "All expected behaviors matched"
            return self.score
        except (ValueError, TypeError) as exc:
            self.error = str(exc)
            raise

    @property
    def __name__(self) -> str:
        return "Helpdesk Critical Behavior"


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
    case: TriageCase | AssessmentCase | EvidenceCase,
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
    evaluation = await client.evaluate_playwright_validation(case.expected_error, case.evidence)
    return evaluation.model_dump()


SUITE = os.environ.get("HELPDESK_AGENT_EVAL_SUITE", "testing")
CASES = select_cases(SUITE)
LABELS = Labels(service="jira", issue_type="access", priority="medium")


def assert_evaluation(test_case: LLMTestCase, metric: BaseMetric) -> None:
    metrics: list[BaseMetric] = [metric]
    assert_test(test_case, metrics, run_async=False)


@pytest.mark.functional
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
async def test_live_triage(case: TriageCase | AssessmentCase | EvidenceCase) -> None:
    config = LLMConfig.from_env()
    if not config.api_key or config.api_key == "test-api-key":
        pytest.fail("Live evaluation requires HELPDESK_AGENT_LLM_API_KEY, not a dummy test key")
    client = LLMTriageClient(config)
    try:
        actual = await run_case(client, case)
    finally:
        await client.client.close()
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
                "model": config.model,
            },
        ),
        HelpdeskBehaviorMetric(case),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "actual,score",
    [
        ({"service": "jira", "issue_type": "access", "priority": "medium"}, 1.0),
        ({"service": "jira", "issue_type": "access", "priority": "low"}, 2 / 3),
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
    assert len([case for case in evaluation if case.id.startswith("baseline-")]) == 12


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
            "summary": "One Jira account is locked.",
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
    case = select_cases("testing")[0]
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
        ("https://status.example.com/health", "Service unavailable", True),
        ("https://invented.example.com/health", "Service unavailable", False),
        ("https://status.example.com/health", "", False),
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
    case = select_cases("testing")[0]
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
