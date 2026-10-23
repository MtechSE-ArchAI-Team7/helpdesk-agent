"""Compatibility exports; implementations live in the canonical package."""

from helpdesk_agent.api.schemas.email import EmailWebhook as EmailWebhook
from helpdesk_agent.api.schemas.email import WebhookResponse as WebhookResponse
from helpdesk_agent.api.schemas.email import _epoch_ms_to_iso as _epoch_ms_to_iso
from helpdesk_agent.api.schemas.email import _HTMLTextExtractor as _HTMLTextExtractor
from helpdesk_agent.api.schemas.email import _strip_html as _strip_html
