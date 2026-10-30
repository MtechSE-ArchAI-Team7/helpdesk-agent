from __future__ import annotations

from pathlib import Path

from evaluations.support.schemas import (
    AssessmentCase,
    Dataset,
    EvaluationCase,
    EvidenceCase,
    PlanningCase,
    ReactCase,
    TriageCase,
)

DATASET_DIR = Path(__file__).resolve().parents[1]


SUITES = ("testing", "evaluation", "security")


def load_cases(path: Path) -> list[EvaluationCase]:
    dataset = Dataset.model_validate_json(path.read_text(encoding="utf-8"))
    cases = list(dataset.cases)
    if dataset.extends:
        if path.name != "triage_evaluation.json":
            raise ValueError("Only the evaluation dataset can extend the testing dataset")
        cases = load_cases(path.with_name("triage_testing.json")) + cases
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be unique")
    for case in cases:
        if case.area is None:
            raise ValueError(f"Case {case.id}: an evaluation area is required")
        fields = {
            "triage": {"summary", "clarifying_questions", "missing_info"},
            "browser_assessment": {"reason", "expected_error", "clarifying_questions", "missing_info"},
            "browser_evidence": {"summary"},
            "browser_planning": {"objective"},
            "browser_react": {"decision_summary"},
        }[case.operation]
        if any(check.field not in fields for check in case.checks):
            raise ValueError(f"Case {case.id}: text check targets a field absent from {case.operation}")
    return cases


def select_cases(suite: str) -> list[TriageCase | AssessmentCase | EvidenceCase | PlanningCase | ReactCase]:
    if suite not in SUITES:
        raise ValueError(f"Unknown evaluation suite {suite!r}; choose testing or evaluation")
    return load_cases(DATASET_DIR / f"triage_{suite}.json")
