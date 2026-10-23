from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends

from helpdesk_agent.api.dependencies import get_workflow
from helpdesk_agent.api.schemas.email import EmailWebhook, WebhookResponse
from helpdesk_agent.workflows.workflow import HelpdeskWorkflow

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/email", response_model=WebhookResponse)
async def email_webhook(
    payload: EmailWebhook,
    workflow: Annotated[HelpdeskWorkflow, Depends(get_workflow)],
) -> dict[str, Any]:
    """Handle incoming email webhook.

    Jira is the source of truth: if the subject contains a ticket ID
    (e.g. [HELP-123]), the workflow loads that ticket from Jira and treats
    the email as new information for it; otherwise a new ticket is created.
    """
    case_id = payload.message_id or f"email-{uuid4().hex[:8]}"
    subject = payload.subject.strip() or "Email request"
    body_text = payload.body.strip()
    raw_text = "\n\n".join([subject, body_text]).strip()

    state = await workflow.run(
        {
            "case_id": case_id,
            "summary": raw_text,
            "subject": subject,
            "source": "email",
            "sender": payload.sender,
            "reply_to": payload.sender,
            "metadata": {
                "message_id": payload.message_id,
                "in_reply_to": payload.in_reply_to,
                "references": payload.references,
                "attachments": payload.attachments,
            },
        }
    )

    return state.to_dict()
