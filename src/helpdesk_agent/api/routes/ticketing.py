from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from helpdesk_agent.api.dependencies import get_workflow
from helpdesk_agent.application.context import ticket_snapshot
from helpdesk_agent.domain.exceptions import MemoryError
from helpdesk_agent.domain.policies.tickets import extract_ticket_id
from helpdesk_agent.workflows.workflow import HelpdeskWorkflow

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class JiraIssueReference(BaseModel):
    key: str = Field(min_length=1, max_length=100)


class JiraMemoryWebhook(BaseModel):
    issue: JiraIssueReference


@router.post("/jira")
async def jira_memory_webhook(
    payload: JiraMemoryWebhook,
    workflow: Annotated[HelpdeskWorkflow, Depends(get_workflow)],
) -> dict[str, Any]:
    """Refresh memory on Jira issue/comment updates; trust only the re-fetched Jira data."""
    memory = workflow.memory
    if memory is None:
        raise HTTPException(status_code=503, detail="AgentCore Memory is disabled")
    ticket_id = payload.issue.key
    if extract_ticket_id(ticket_id, workflow.project_key) != ticket_id:
        raise HTTPException(status_code=400, detail="Ticket key does not match the configured Jira project")
    if not workflow.mcp_enabled:
        raise HTTPException(status_code=503, detail="Jira MCP must be enabled to ingest ticket updates")
    ticket = await workflow.ticket_flow.get_ticket(ticket_id)
    if ticket.get("status") != "success":
        raise HTTPException(status_code=502, detail="Could not retrieve the Jira ticket for memory ingestion")
    snapshot = {"snapshot_origin": "jira_update", **ticket_snapshot(ticket)}
    identity = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
    try:
        await memory.save_snapshot(ticket_id, snapshot, f"jira-update:{identity}")
    except MemoryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ticket_id": ticket_id, "status": "submitted", "extraction": "asynchronous"}
