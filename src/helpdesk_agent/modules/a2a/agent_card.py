"""Build the public Agent Card describing this helpdesk agent."""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from ...config import A2AConfig

# JSON-RPC mount path; must match the rpc_url used in main.create_app().
A2A_RPC_PATH = "/a2a"


def build_agent_card(config: A2AConfig) -> AgentCard:
    """Create the AgentCard advertised at /.well-known/agent-card.json."""
    return AgentCard(
        name=config.agent_name,
        description=(
            "Helpdesk triage agent: classifies IT support requests, answers from "
            "the FAQ knowledge base, files Jira tickets, and manages approvals."
        ),
        url=f"{config.base_url}{A2A_RPC_PATH}",
        version="0.1.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            state_transition_history=False,
        ),
        skills=[
            AgentSkill(
                id="triage-helpdesk-request",
                name="Triage helpdesk request",
                description=(
                    "Classify an IT support request, resolve it from the FAQ when "
                    "possible, or file and track a Jira ticket (with approval when "
                    "production-impacting)."
                ),
                tags=["helpdesk", "triage", "it-support"],
                examples=[
                    "VPN is down for the Berlin office",
                    "I need to reset my password",
                    "Laptop won't boot after the latest update",
                ],
            ),
            AgentSkill(
                id="check-case-status",
                name="Check case status",
                description=(
                    "Follow up on an existing helpdesk case within the same conversation context (contextId)."
                ),
                tags=["helpdesk", "status"],
                examples=["Any update on my ticket?"],
            ),
        ],
    )
