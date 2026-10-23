from __future__ import annotations

import logging
from typing import Any

from helpdesk_agent.application.context import ticket_snapshot
from helpdesk_agent.domain.exceptions import MemoryError
from helpdesk_agent.domain.models.workflow import WorkflowState
from helpdesk_agent.ports.capabilities import MemoryPort

logger = logging.getLogger(__name__)


def _memory_failure(state: WorkflowState, step: str, error: MemoryError) -> None:
    logger.error("case=%s stage=%s status=failed error=%s", state.case_id, step, error)
    state.record_history(step, "failed", str(error))
    state.metadata.setdefault("memory_errors", []).append({"operation": step, "error": str(error)})


async def save_chat_response(memory: MemoryPort | None, state: WorkflowState, content: str, identity: str) -> None:
    if memory and state.ticket and state.current_step != "ticket_creation_failed":
        try:
            await memory.save_message(state.case_id, "ASSISTANT", content, identity)
        except MemoryError as exc:
            _memory_failure(state, "memory_save_response", exc)


async def _save_incoming_memory(
    memory: MemoryPort | None, state: WorkflowState, initial_input: dict[str, Any], ticket_id: str
) -> None:
    if memory is None:
        return
    try:
        await memory.save_message(ticket_id, "USER", initial_input.get("summary", ""), initial_input["memory_identity"])
        initial_input["memory_user_saved"] = True
        state.record_history("memory_save_user", "completed", "Saved incoming ticket report")
    except MemoryError as exc:
        _memory_failure(state, "memory_save_user", exc)


async def _save_ticket_memory(
    memory: MemoryPort | None, state: WorkflowState, initial_input: dict[str, Any], existing: dict[str, Any]
) -> None:
    if memory is None:
        return
    identity = initial_input["memory_identity"]
    snapshot = {
        **ticket_snapshot(existing),
        "report": initial_input.get("summary", "")[:10000],
        "triage_summary": state.summary[:2000],
        "service": state.service,
        "workflow_step": state.current_step,
        "clarifying_questions": (state.clarification or {}).get("questions", []),
        "diagnostics": state.evidence,
        "suggested_faq": [
            {"title": item.get("title"), "body": str(item.get("body", ""))[:4000]} for item in state.faq_matches[:1]
        ],
    }
    try:
        await memory.save_snapshot(state.case_id, snapshot, identity)
        state.record_history("memory_snapshot", "completed", "Submitted case snapshot for asynchronous extraction")
    except MemoryError as exc:
        _memory_failure(state, "memory_snapshot", exc)
