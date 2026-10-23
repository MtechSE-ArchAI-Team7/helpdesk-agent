from __future__ import annotations

import re
from typing import Any


def redact_shared_text(text: str) -> str:
    """Defense in depth, not a complete PII classifier; shared tickets must be approved for this organization."""
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED]", text, flags=re.S)
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL]", text, flags=re.I)
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTED]", text)
    text = re.sub(r"\bBearer\s+\S+", "Bearer [REDACTED]", text, flags=re.I)
    text = re.sub(
        r"""((?:password|passwd|api[_-]?key|access[_-]?token|secret|authorization)\s*["']?\s*[:=]\s*)"""
        r"""(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
        r"\1[REDACTED]",
        text,
        flags=re.I,
    )
    return text


def bounded_snippets(snippets: list[str], limit: int) -> list[str]:
    result: list[str] = []
    remaining = limit
    for snippet in snippets:
        if remaining <= 0:
            break
        result.append(snippet[:remaining])
        remaining -= len(result[-1])
    return result


def shared_snapshot(value: Any) -> Any:
    if isinstance(value, str):
        return redact_shared_text(value)
    if isinstance(value, dict):
        return {key: shared_snapshot(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shared_snapshot(item) for item in value]
    return value


def ticket_snapshot(ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_summary": str(ticket.get("summary", ""))[:2000],
        "ticket_description": str(ticket.get("description", ""))[:10000],
        "ticket_comments": [
            str(comment.get("body", "") if isinstance(comment, dict) else comment)[:2000]
            for comment in (ticket.get("comments", []) or [])[-10:]
        ],
        "issue_status": ticket.get("issue_status"),
        "resolution": ticket.get("resolution"),
        "resolution_verified": False,
    }
