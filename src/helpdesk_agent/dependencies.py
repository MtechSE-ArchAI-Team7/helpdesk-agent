"""Compatibility exports; implementations live in the canonical package."""

from helpdesk_agent.api.dependencies import get_a2a_client_manager as get_a2a_client_manager
from helpdesk_agent.api.dependencies import get_a2a_config as get_a2a_config
from helpdesk_agent.api.dependencies import get_conversation_store as get_conversation_store
from helpdesk_agent.api.dependencies import get_llm_client as get_llm_client
from helpdesk_agent.api.dependencies import get_llm_config as get_llm_config
from helpdesk_agent.api.dependencies import get_mcp_adapter as get_mcp_adapter
from helpdesk_agent.api.dependencies import get_mcp_config as get_mcp_config
from helpdesk_agent.api.dependencies import get_memory as get_memory
from helpdesk_agent.api.dependencies import get_workflow as get_workflow
