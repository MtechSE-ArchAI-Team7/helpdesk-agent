from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkflowState:
    case_id: str
    summary: str
    service: str = "unknown"
    current_step: str = "triage"
    messages: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    ticket: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    triage: dict[str, Any] | None = None
    faq_matches: list[dict[str, Any]] = field(default_factory=list)
    clarification: dict[str, Any] | None = None

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def record_history(self, step: str, status: str, detail: str) -> None:
        self.history.append({"step": step, "status": status, "detail": detail})

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "summary": self.summary,
            "service": self.service,
            "current_step": self.current_step,
            "messages": self.messages,
            "evidence": self.evidence,
            "ticket": self.ticket,
            "metadata": self.metadata,
            "history": self.history,
            "triage": self.triage,
            "faq_matches": self.faq_matches,
            "clarification": self.clarification,
        }
