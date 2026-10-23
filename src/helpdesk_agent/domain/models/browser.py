from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


class PlaywrightAssessment(BaseModel):
    decision: Literal["eligible", "needs_info", "not_applicable"]
    reason: str
    target_url: str | None = None
    expected_error: str = ""
    clarifying_questions: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)


class PlaywrightEvaluation(BaseModel):
    status: Literal["reproduced", "not_reproduced", "inconclusive"]
    summary: str


@dataclass(frozen=True)
class BrowserSettings:
    runtime_configured: bool = False
    allowed_hosts: tuple[str, ...] = ()
