"""Compatibility exports for ticket memory."""

from helpdesk_agent.application.context import bounded_snippets as bounded_snippets
from helpdesk_agent.application.context import redact_shared_text as redact_shared_text
from helpdesk_agent.application.context import shared_snapshot as shared_snapshot
from helpdesk_agent.application.context import ticket_snapshot as ticket_snapshot
from helpdesk_agent.domain.exceptions import MemoryError as MemoryError
from helpdesk_agent.infrastructure.memory.agentcore import FACTS_NAMESPACE as FACTS_NAMESPACE
from helpdesk_agent.infrastructure.memory.agentcore import SUMMARY_NAMESPACE as SUMMARY_NAMESPACE
from helpdesk_agent.infrastructure.memory.agentcore import AgentCoreMemory as AgentCoreMemory
from helpdesk_agent.infrastructure.memory.agentcore import MemoryClient as MemoryClient
