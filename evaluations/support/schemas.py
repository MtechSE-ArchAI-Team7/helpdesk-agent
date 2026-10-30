from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helpdesk_agent.domain.models.browser import (
    BrowserInspectionTool,
    PlaywrightAssessment,
)
from helpdesk_agent.domain.policies.priority import Priority

Service = Literal["jira", "slack", "email", "identity", "platform", "prometheus", "unknown"]


IssueType = Literal["access", "incident", "performance", "deployment", "general"]


Area = Literal["security", "incident_types", "ambiguity", "user_roles", "routing", "priority", "classification"]


AREA_COUNTS = {
    "security": 12,
    "incident_types": 8,
    "ambiguity": 8,
    "user_roles": 8,
    "routing": 8,
    "priority": 8,
    "classification": 8,
}


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


class PlanningExpected(StrictModel):
    allowed_tools: list[BrowserInspectionTool] = Field(min_length=1)


class ReactExpected(StrictModel):
    action: Literal["inspect", "finish"]
    tool: BrowserInspectionTool | None = None

    @model_validator(mode="after")
    def match_action_to_tool(self) -> ReactExpected:
        if (self.action == "inspect") != (self.tool is not None):
            raise ValueError("Inspect expectations require a tool; finish expectations must omit it")
        return self


class CaseBase(StrictModel):
    id: str = Field(min_length=1)
    area: Area | None = None
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


class PlanningCase(CaseBase):
    operation: Literal["browser_planning"]
    target_url: str = Field(min_length=1)
    expected_error: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    expected: PlanningExpected


class ReactCase(CaseBase):
    operation: Literal["browser_react"]
    target_url: str = Field(min_length=1)
    expected_error: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    inspection_order: list[BrowserInspectionTool] = Field(min_length=1, max_length=3)
    observations: list[dict[str, object]] = Field(min_length=1)
    round_number: int = Field(ge=1, le=3)
    expected: ReactExpected


EvaluationCase = Annotated[
    TriageCase | AssessmentCase | EvidenceCase | PlanningCase | ReactCase,
    Field(discriminator="operation"),
]


class Dataset(StrictModel):
    extends: Literal["testing"] | None = None
    cases: list[EvaluationCase] = Field(min_length=1)
