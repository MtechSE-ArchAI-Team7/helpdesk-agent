import re


def extract_ticket_id(subject: str, project_key: str = "HELP") -> str | None:
    """Extract a Jira ticket ID (e.g. HELP-123) from an email subject.

    Only matches the configured project key, so unrelated PROJECT-123 style
    strings in subjects do not trigger the existing-ticket path.
    """
    match = re.search(rf"\b{re.escape(project_key)}-\d+\b", subject or "")
    return match.group(0) if match else None
