"""Compatibility exports; implementations live in the canonical package."""

from helpdesk_agent.api.routes.ticketing import JiraIssueReference as JiraIssueReference
from helpdesk_agent.api.routes.ticketing import JiraMemoryWebhook as JiraMemoryWebhook
from helpdesk_agent.api.routes.ticketing import jira_memory_webhook as jira_memory_webhook
from helpdesk_agent.api.routes.ticketing import router as router
