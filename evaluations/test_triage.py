from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from deepeval import assert_test
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase
from pydantic import BaseModel, ConfigDict, Field

from helpdesk_agent.config import LLMConfig
from helpdesk_agent.modules.triage.llm import LLMTriageClient


class Labels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Literal["jira", "slack", "email", "identity", "platform", "prometheus", "unknown"]
    issue_type: Literal["access", "incident", "performance", "deployment", "general"]
    priority: Literal["critical", "high", "medium", "low"]


class TriageCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    expected: Labels


def load_cases(path: Path) -> list[TriageCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("Evaluation dataset must be a nonempty list")
    cases = [TriageCase.model_validate(row) for row in data]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be unique")
    return cases


class TriageLabelMetric(BaseMetric):
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
        return self.error is None and self.success

    @property
    def __name__(self) -> str:
        return "Triage Label Accuracy"


CASES = load_cases(Path(__file__).with_name("triage_cases.json"))


@pytest.mark.functional
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
async def test_live_triage(case: TriageCase) -> None:
    config = LLMConfig.from_env()
    if not config.api_key or config.api_key == "test-api-key":
        pytest.fail("Live evaluation requires HELPDESK_AGENT_LLM_API_KEY, not a dummy test key")
    client = LLMTriageClient(config)
    try:
        result = await client.triage_email(case.subject, case.body)
    finally:
        await client.client.close()
    actual = {field: getattr(result, field) for field in Labels.model_fields}
    assert_test(
        LLMTestCase(
            input=f"{case.subject}\n\n{case.body}",
            actual_output=json.dumps(actual),
            expected_output=case.expected.model_dump_json(),
        ),
        [TriageLabelMetric()],
        run_async=False,
    )


@pytest.mark.unit
@pytest.mark.parametrize("actual,score", [
    ({"service": "jira", "issue_type": "access", "priority": "medium"}, 1.0),
    ({"service": "jira", "issue_type": "access", "priority": "low"}, 2 / 3),
    ({}, 0.0),
])
def test_metric_label_scores(actual: dict[str, str], score: float) -> None:
    metric = TriageLabelMetric()
    case = LLMTestCase(
        input="test", actual_output=json.dumps(actual), expected_output=CASES[0].expected.model_dump_json()
    )
    assert metric.measure(case) == pytest.approx(score)
    assert metric.is_successful() == (score == 1.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metric_async_parity() -> None:
    labels = CASES[0].expected.model_dump_json()
    case = LLMTestCase(input="test", actual_output=labels, expected_output=labels)
    metric = TriageLabelMetric()
    assert await metric.a_measure(case) == metric.measure(case) == 1.0


@pytest.mark.unit
def test_deepeval_assertion_pass_and_failure() -> None:
    labels = CASES[0].expected.model_dump_json()
    assert_test(
        LLMTestCase(input="offline", actual_output=labels, expected_output=labels),
        [TriageLabelMetric()],
        run_async=False,
    )
    with pytest.raises(AssertionError):
        assert_test(
            LLMTestCase(input="offline", actual_output="{}", expected_output=labels),
            [TriageLabelMetric()],
            run_async=False,
        )


@pytest.mark.unit
@pytest.mark.parametrize("actual", ["invalid JSON", "[]"])
def test_metric_rejects_invalid_output(actual: str) -> None:
    metric = TriageLabelMetric()
    with pytest.raises(ValueError):
        metric.measure(
            LLMTestCase(input="test", actual_output=actual, expected_output=CASES[0].expected.model_dump_json())
        )
    assert not metric.is_successful()


@pytest.mark.unit
@pytest.mark.parametrize("data", [[], [{"id": "incomplete"}], [CASES[0].model_dump()] * 2])
def test_dataset_rejects_invalid_cases(tmp_path: Path, data: list[object]) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)