from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import uuid4


@dataclass
class WorkflowState:
    case_id: str
    summary: str
    run_id: str = field(default_factory=lambda: str(uuid4()))
    case_reference: str = field(init=False)
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
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.case_reference = f"case-{sha256(self.case_id.encode('utf-8')).hexdigest()[:16]}"

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def record_history(self, step: str, status: str, detail: str) -> None:
        self.history.append({"step": step, "status": status, "detail": detail})

    def record_event(
        self,
        *,
        event_type: str,
        stage: str,
        actor_type: str,
        status: str,
        summary: str,
        trace_id: str | None = None,
        duration_ms: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": "1.0",
            "event_type": "helpdesk.workflow.event",
            "event_id": str(uuid4()),
            "run_id": self.run_id,
            "case_id": self.case_reference,
            "trace_id": trace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "actor_type": actor_type,
            "action": event_type,
            "status": status,
            "summary": summary,
        }
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        if details:
            safe_details = dict(details)
            ticket_id = safe_details.get("ticket_id")
            if ticket_id is not None and (
                not isinstance(ticket_id, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}-[0-9]{1,12}", ticket_id) is None
            ):
                safe_details.pop("ticket_id")
            event["details"] = safe_details
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
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
            "events": self.events,
        }
