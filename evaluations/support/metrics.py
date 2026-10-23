from __future__ import annotations

import json
import re

from deepeval.evaluate import assert_test
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase

from evaluations.support.schemas import (
    AssessmentCase,
    EvidenceCase,
    Labels,
    TriageCase,
)
from helpdesk_agent.domain.models.browser import PlaywrightAssessment, PlaywrightEvaluation
from helpdesk_agent.domain.models.triage import TriageLLMResponse
from helpdesk_agent.domain.policies.browser import BrowserTargetPolicy


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
                    target not in BrowserTargetPolicy.reported_urls(report)
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


def assert_evaluation(test_case: LLMTestCase, metric: BaseMetric) -> None:
    metrics: list[BaseMetric] = [metric]
    assert_test(test_case, metrics, run_async=False)
