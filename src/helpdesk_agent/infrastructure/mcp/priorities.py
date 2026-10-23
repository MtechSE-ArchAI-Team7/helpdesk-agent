from __future__ import annotations

_JIRA_PRIORITY_MAP = {
    "highest": "Highest",
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "lowest": "Lowest",
}


def to_jira_priority(priority: str) -> str:
    """Map canonical priorities and legacy critical; pass through custom Jira names."""
    return _JIRA_PRIORITY_MAP.get(priority.strip().lower(), priority.strip().title())
