from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from helpdesk_agent.workflows.context import WorkflowContext
from helpdesk_agent.workflows.state import GraphState

logger = logging.getLogger(__name__)


#: Marker used in Jira comments so clarification rounds can be counted from the ticket.


#: Name of the remote diagnosis agent in A2A remote-agent configuration.


def build_graph(self: WorkflowContext) -> Any:
    """Build the LangGraph state graph."""
    graph = StateGraph(GraphState)

    # Add nodes
    graph.add_node("load_ticket", self._load_ticket_node)
    graph.add_node("create_ticket", self._create_ticket_node)
    graph.add_node("acknowledge", self._acknowledge_node)
    graph.add_node("triage", self._triage_node)
    graph.add_node("update_ticket", self._update_ticket_node)
    graph.add_node("assess_playwright", self._assess_playwright_node)
    graph.add_node("check_clarification", self._check_clarification_node)
    graph.add_node("clarify", self._clarify_node)
    graph.add_node("playwright_validate", self._playwright_validate_node)
    graph.add_node("faq_gate", self._faq_gate_node)
    graph.add_node("resolve_faq", self._resolve_faq_node)
    graph.add_node("diagnose", self._diagnose_node)
    graph.add_node("email_read", self._email_read_node)

    # Set entry point
    graph.set_entry_point("load_ticket")

    # Add edges
    graph.add_conditional_edges(
        "load_ticket",
        self._has_existing_ticket,
        {
            "existing": "triage",
            "new": "create_ticket",
        },
    )
    graph.add_conditional_edges(
        "create_ticket",
        self._ticket_created,
        {
            "created": "acknowledge",
            "failed": END,
        },
    )
    graph.add_edge("acknowledge", "triage")
    graph.add_edge("triage", "update_ticket")
    graph.add_edge("update_ticket", "assess_playwright")
    graph.add_edge("assess_playwright", "check_clarification")
    graph.add_conditional_edges(
        "check_clarification",
        self._should_clarify,
        {
            "clarify": "clarify",
            "diagnose": "diagnose",
            "playwright": "playwright_validate",
            "faq_gate": "faq_gate",
        },
    )
    graph.add_edge("clarify", END)
    graph.add_edge("playwright_validate", "faq_gate")
    graph.add_conditional_edges(
        "faq_gate",
        self._should_resolve_faq,
        {
            "resolve_faq": "resolve_faq",
            "diagnose": "diagnose",
        },
    )
    graph.add_edge("resolve_faq", END)
    graph.add_edge("diagnose", END)
    graph.add_edge("email_read", END)

    return graph.compile()
