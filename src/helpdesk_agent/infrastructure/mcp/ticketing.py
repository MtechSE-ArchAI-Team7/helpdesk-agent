from __future__ import annotations

from typing import Any

from helpdesk_agent.config.settings import MCPConfig
from helpdesk_agent.domain.policies.tickets import extract_ticket_id
from helpdesk_agent.infrastructure.mcp.transport import MCPProxyAdapter, MCPResponse

# Triage priorities -> Jira priority names (Jira standard set).
_JIRA_PRIORITY_MAP = {
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "lowest": "Lowest",
}


def to_jira_priority(priority: str) -> str:
    """Map a triage priority value to a Jira priority name.

    Unknown values are title-cased and passed through (custom Jira priority
    schemes may define them).
    """
    return _JIRA_PRIORITY_MAP.get(priority.strip().lower(), priority.strip().title())


class JiraTicketFlow:
    def __init__(self, mcp_adapter: MCPProxyAdapter | None = None) -> None:
        self.mcp_adapter = mcp_adapter

    async def create_ticket(
        self,
        case_id: str,
        summary: str,
        description: str,
        service: str,
        priority: str,
        reporter: str,
    ) -> dict[str, Any]:
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "ticket_id": f"JIRA-{case_id}",
                "ticket_url": f"https://jira.example.com/browse/JIRA-{case_id}",
            }

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "jira.create_issue",
            {
                "case_id": case_id,
                "summary": summary,
                "description": description,
                "service": service,
                "priority": priority,
                "reporter": reporter,
            },
            wait_for_completion=True,
            idempotency_key=f"create-ticket-{case_id}",
            policy={"request_type": "ticket_creation"},
        )

        if response.status != "completed":
            return {"status": "failed", "action_id": response.action_id, "error": response.error}

        # Real mcp-atlassian returns the created issue (key, e.g. "HELP-123"),
        # possibly nested (issue.key) or embedded in a raw text blob; the legacy
        # mock returns content.arguments.case_id. Never invent an ID here: if no
        # key is extractable, the ticket may exist in Jira but we cannot address
        # it, so report failure instead of corrupting every downstream call.
        content = response.content
        project_key = MCPConfig.from_env().jira_project_key
        ticket_id = (
            content.get("key")
            or content.get("issue_key")
            or (content.get("issue") or {}).get("key")
            or content.get("arguments", {}).get("case_id")
            or extract_ticket_id(str(content.get("text", "")), project_key)
            or extract_ticket_id(str(content), project_key)
        )
        if not ticket_id:
            return {
                "status": "failed",
                "action_id": response.action_id,
                "error": "jira.create_issue succeeded but no issue key found in response",
            }
        ticket_url = content.get("url") or content.get("browse_url", f"https://jira.example.com/browse/{ticket_id}")
        ticket_info: dict[str, Any] = {
            "status": "created",
            "ticket_id": ticket_id,
            "ticket_url": ticket_url,
            "action_id": response.action_id,
        }
        return ticket_info

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Fetch an existing Jira ticket, including its comments."""
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "ticket_id": ticket_id,
                "summary": f"Mock summary for {ticket_id}",
                "description": "Mock description",
                "priority": "medium",
                "comments": [],
            }

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "jira.get_issue",
            {"issue_key": ticket_id},
            wait_for_completion=True,
        )

        if response.status != "completed":
            return {"status": "failed", "ticket_id": ticket_id, "error": response.error}

        content = response.content
        return {
            "status": "success",
            "ticket_id": content.get("key") or content.get("issue_key") or ticket_id,
            "summary": content.get("summary", ""),
            "description": content.get("description", ""),
            "priority": content.get("priority", ""),
            "comments": content.get("comments", []),
            "issue_status": content.get("status"),
            "resolution": content.get("resolution"),
        }

    async def add_comment(self, ticket_id: str, body: str) -> dict[str, Any]:
        """Add a comment to a Jira ticket. All workflow ticket updates go through this."""
        if self.mcp_adapter is None:
            return {"status": "mocked", "ticket_id": ticket_id, "comment": body}

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "jira.add_comment",
            {"issue_key": ticket_id, "comment": body},
            wait_for_completion=True,
            idempotency_key=f"comment-{ticket_id}-{hash(body) & 0xFFFFFFFF:x}",
        )

        if response.status != "completed":
            return {"status": "failed", "ticket_id": ticket_id, "error": response.error}

        return {"status": "commented", "ticket_id": ticket_id}

    async def update_ticket(
        self,
        ticket_id: str,
        *,
        summary: str | None = None,
        description: str | None = None,
        priority: str | None = None,
    ) -> dict[str, Any]:
        """Update a Jira ticket's fields to reflect the latest triage results."""
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "ticket_id": ticket_id,
                "updated": {
                    k: v
                    for k, v in {"summary": summary, "description": description, "priority": priority}.items()
                    if v is not None
                },
            }

        fields: dict[str, Any] = {}
        if summary is not None:
            fields["summary"] = summary
        if description is not None:
            fields["description"] = description
        if priority is not None:
            fields["priority"] = to_jira_priority(priority)

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "jira.update_issue",
            {"issue_key": ticket_id, "fields": fields},
            wait_for_completion=True,
        )

        if response.status != "completed":
            return {"status": "failed", "ticket_id": ticket_id, "error": response.error}

        return {"status": "updated", "ticket_id": ticket_id, "updated": list(fields)}
