from __future__ import annotations

from functools import lru_cache

from .config import A2AConfig, LLMConfig, MCPConfig
from .core.workflow import HelpdeskWorkflow
from .mcp import MCPProxyAdapter
from .modules.a2a.client import A2AClientManager
from .modules.chat.service import ConversationStore
from .modules.triage.llm import LLMTriageClient


@lru_cache
def get_mcp_config() -> MCPConfig:
    """Get MCP configuration from environment."""
    return MCPConfig.from_env()


@lru_cache
def get_llm_config() -> LLMConfig:
    """Get LLM configuration from environment."""
    return LLMConfig.from_env()


@lru_cache
def get_mcp_adapter() -> MCPProxyAdapter | None:
    """Get MCP adapter if enabled, None otherwise."""
    import os

    use_mcp = os.getenv("HELPDESK_AGENT_USE_MCP", "0") == "1"
    if use_mcp:
        return MCPProxyAdapter(get_mcp_config())
    return None


def get_llm_client() -> LLMTriageClient:
    """Get LLM client instance. Not cached to allow mocking in tests."""
    return LLMTriageClient(get_llm_config())


def get_workflow() -> HelpdeskWorkflow:
    """Get workflow instance with optional MCP adapter and LLM client."""
    return HelpdeskWorkflow(get_mcp_adapter(), get_llm_client(), a2a_client_manager=get_a2a_client_manager())


@lru_cache
def get_conversation_store() -> ConversationStore:
    """Get conversation store singleton."""
    return ConversationStore()


@lru_cache
def get_a2a_config() -> A2AConfig:
    """Get A2A configuration from environment."""
    return A2AConfig.from_env()


@lru_cache
def get_a2a_client_manager() -> A2AClientManager:
    """Get the outbound A2A client manager singleton (plumbing only)."""
    return A2AClientManager(get_a2a_config())
