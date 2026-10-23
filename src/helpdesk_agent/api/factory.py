from __future__ import annotations

from helpdesk_agent.config import MCPConfig, TriageConfig
from helpdesk_agent.domain.models.browser import BrowserSettings
from helpdesk_agent.infrastructure.a2a.client import A2AClientManager
from helpdesk_agent.infrastructure.knowledge.faq import FAQFlow
from helpdesk_agent.infrastructure.llm.client import LLMTriageClient
from helpdesk_agent.infrastructure.mcp.browser import PlaywrightValidationFlow
from helpdesk_agent.infrastructure.mcp.email import ZohoMailFlow
from helpdesk_agent.infrastructure.mcp.ticketing import JiraTicketFlow
from helpdesk_agent.infrastructure.mcp.transport import MCPProxyAdapter
from helpdesk_agent.infrastructure.memory.agentcore import AgentCoreMemory
from helpdesk_agent.workflows.state import GraphState as GraphState
from helpdesk_agent.workflows.workflow import (
    CLARIFICATION_COMMENT_MARKER as CLARIFICATION_COMMENT_MARKER,
)
from helpdesk_agent.workflows.workflow import (
    DIAGNOSIS_AGENT_NAME as DIAGNOSIS_AGENT_NAME,
)
from helpdesk_agent.workflows.workflow import (
    HelpdeskWorkflow as ComposedWorkflow,
)


class HelpdeskWorkflow(ComposedWorkflow):
    """Retain the historical optional-client constructor at the composition root."""

    def __init__(
        self,
        mcp_adapter: MCPProxyAdapter | None = None,
        llm_client: LLMTriageClient | None = None,
        triage_config: TriageConfig | None = None,
        a2a_client_manager: A2AClientManager | None = None,
        memory: AgentCoreMemory | None = None,
    ) -> None:
        self.mcp_adapter = mcp_adapter
        browser = PlaywrightValidationFlow(mcp_adapter)
        super().__init__(
            llm_client=llm_client or LLMTriageClient(),
            mail_flow=ZohoMailFlow(mcp_adapter),
            ticket_flow=JiraTicketFlow(mcp_adapter),
            faq_flow=FAQFlow(mcp_adapter),
            playwright_flow=browser,
            triage_config=triage_config or TriageConfig.from_env(),
            project_key=MCPConfig.from_env().jira_project_key,
            mcp_enabled=mcp_adapter is not None,
            browser_settings=BrowserSettings(
                bool(browser.config.playwright_url), tuple(browser.config.playwright_allowed_hosts)
            ),
            a2a_client_manager=a2a_client_manager,
            memory=memory,
        )
