from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TriageLLMResponse(BaseModel):
    """Constrained response contract for the triage model."""

    service: Literal["jira", "slack", "email", "identity", "platform", "prometheus", "unknown"]
    issue_type: Literal["access", "incident", "performance", "deployment", "general"]
    priority: Literal["critical", "high", "medium", "low"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    missing_info: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    summary: str


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
