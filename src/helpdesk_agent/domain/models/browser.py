from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


BrowserInspectionTool = Literal[
    "browser_snapshot",
    "browser_console_messages",
    "browser_network_requests",
]


class BrowserPlan(BaseModel):
    objective: str = Field(min_length=1, max_length=500)
    inspection_order: list[BrowserInspectionTool] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_inspections(self) -> BrowserPlan:
        if len(self.inspection_order) != len(set(self.inspection_order)):
            raise ValueError("Browser inspection tools must not repeat in the plan")
        return self


class BrowserReactAction(BaseModel):
    action: Literal["inspect", "finish"]
    tool: BrowserInspectionTool | None = None
    decision_summary: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def match_action_to_tool(self) -> BrowserReactAction:
        if (self.action == "inspect") != (self.tool is not None):
            raise ValueError("Inspect actions require an allowed tool; finish actions must not include a tool")
        return self


@dataclass(frozen=True)
class BrowserSettings:
    runtime_configured: bool = False
    allowed_hosts: tuple[str, ...] = ()
