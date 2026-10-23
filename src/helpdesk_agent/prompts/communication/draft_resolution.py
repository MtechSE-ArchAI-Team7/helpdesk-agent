from __future__ import annotations

from typing import Any


def render_fallback_resolution(
    faq_article: dict[str, Any],
    raw_text: str,
) -> str:
    """
    Draft a resolution email based on an FAQ article.

    Args:
        faq_article: The FAQ article with title and body
        raw_text: Original email text

    Returns:
        Drafted resolution email body
    """
    title = faq_article.get("title", "Solution")
    body = faq_article.get("body", "")

    return f"""Thank you for contacting support.

We found a solution that may resolve your issue:

**{title}**

{body}

If this doesn't resolve your issue, please reply to this email and we'll create a support ticket for further assistance.

Best regards,
Helpdesk Team"""
