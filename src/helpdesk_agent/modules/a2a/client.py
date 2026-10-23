"""Compatibility exports; implementations live in the canonical package."""

from helpdesk_agent.infrastructure.a2a.client import A2A_VERSION_HEADER as A2A_VERSION_HEADER
from helpdesk_agent.infrastructure.a2a.client import A2AClientManager as A2AClientManager
from helpdesk_agent.infrastructure.a2a.client import DeliveryOutcome as DeliveryOutcome
from helpdesk_agent.infrastructure.a2a.client import SendMessageEvent as SendMessageEvent
from helpdesk_agent.infrastructure.a2a.client import _extract_task_id as _extract_task_id
from helpdesk_agent.infrastructure.a2a.client import _try_parse_json as _try_parse_json
from helpdesk_agent.infrastructure.a2a.client import logger as logger
