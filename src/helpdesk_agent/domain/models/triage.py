from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class TriageLLMResponse(BaseModel):
    """Constrained response contract for the triage model."""

    service: Literal["jira", "slack", "email", "identity", "platform", "prometheus", "unknown"]
    issue_type: Literal["access", "incident", "performance", "deployment", "general"]
    priority: Literal["critical", "high", "medium", "low"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    missing_info: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    summary: str


class EmailDraft(BaseModel):
    body: str = Field(min_length=1, max_length=12000)


ReflectionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ReflectionRevision(TriageLLMResponse):
    model_config = ConfigDict(extra="forbid")

    summary: ReflectionText
    missing_info: list[ReflectionText] = Field(default_factory=list, max_length=10)
    clarifying_questions: list[ReflectionText] = Field(default_factory=list, max_length=10)


class TriageReflection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["accept", "revise", "uncertain"]
    assessment_summary: ReflectionText
    findings: list[ReflectionText] = Field(default_factory=list, max_length=10)
    revised_triage: ReflectionRevision | None = None
    missing_info: list[ReflectionText] = Field(default_factory=list, max_length=10)
    clarifying_questions: list[ReflectionText] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_outcome(self) -> TriageReflection:
        if self.outcome == "revise":
            if self.revised_triage is None:
                raise ValueError("Revise requires a complete corrected triage")
        elif self.revised_triage is not None:
            raise ValueError("Only revise may supply a corrected triage")
        if self.outcome == "uncertain":
            if not self.missing_info or not self.clarifying_questions:
                raise ValueError("Uncertain requires missing information and clarification questions")
        elif self.missing_info or self.clarifying_questions:
            raise ValueError("Only uncertain may supply separate clarification details")
        return self


class TriageResult(BaseModel):
    """Structured output from LLM triage."""

    service: str = Field(..., description="Detected service (e.g., jira, slack, email, identity)")
    issue_type: str = Field(..., description="Issue type (e.g., access, incident, performance, general)")
    priority: str = Field(..., description="Priority level (critical, high, medium, low)")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score 0-1")
    missing_info: list[str] = Field(default_factory=list, description="Information still needed")
    clarifying_questions: list[str] = Field(default_factory=list, description="Questions to ask user")
    summary: str = Field(..., description="Brief summary of the issue")
    source: str = Field(default="email", description="Source of the request")
    sender: str = Field(default="unknown", description="Sender identifier")
    raw_text: str = Field(default="", description="Original text")
