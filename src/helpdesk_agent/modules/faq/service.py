from __future__ import annotations

from typing import Any

from ...mcp import MCPProxyAdapter, MCPResponse


class FAQFlow:
    """Handle FAQ search operations via MCP."""

    def __init__(self, mcp_adapter: MCPProxyAdapter | None = None) -> None:
        self.mcp_adapter = mcp_adapter

    async def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Search FAQ repository via MCP."""
        if self.mcp_adapter is None:
            return {
                "status": "mocked",
                "results": [],
                "count": 0,
            }

        response: MCPResponse = await self.mcp_adapter.call_tool(
            "faq.search",
            {
                "query": query,
                "max_results": max_results,
            },
            wait_for_completion=True,
        )

        if response.status != "completed":
            return {"status": "failed", "action_id": response.action_id, "error": response.error}

        results = response.content.get("results", [])
        if self.mcp_adapter.config.faq_tool_prefix is not None:
            results = [
                {
                    **item,
                    "title": (item.get("metadata") or {}).get("title", "Operational knowledge excerpt"),
                    "body": item.get("text", ""),
                }
                for item in results
            ]

        return {
            "status": "success",
            "results": results,
            "count": response.content.get("resultCount", response.content.get("count", len(results))),
            "action_id": response.action_id,
        }
