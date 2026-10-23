"""Compatibility exports; implementations live in the canonical package."""

from helpdesk_agent.infrastructure.mcp.transport import GatewayIAMAuth as GatewayIAMAuth
from helpdesk_agent.infrastructure.mcp.transport import MCPProxyAdapter as MCPProxyAdapter
from helpdesk_agent.infrastructure.mcp.transport import MCPResponse as MCPResponse
from helpdesk_agent.infrastructure.mcp.transport import _content_to_dict as _content_to_dict
from helpdesk_agent.infrastructure.mcp.transport import _root_exception as _root_exception
from helpdesk_agent.infrastructure.mcp.transport import _safe_error_detail as _safe_error_detail
from helpdesk_agent.infrastructure.mcp.transport import _unwrap_error as _unwrap_error
from helpdesk_agent.infrastructure.mcp.transport import logger as logger
