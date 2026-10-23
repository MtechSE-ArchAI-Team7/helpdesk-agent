from __future__ import annotations

from helpdesk_agent.domain.policies.tickets import extract_ticket_id


def _ticketed_subject(ticket_id: str | None, subject: str, project_key: str) -> str:
    """Prefix the subject with [TICKET-ID] unless it already contains a
    ticket ID (e.g. a "Re: [AH-23] ..." reply), to avoid stacking prefixes
    like "[AH-23] Re: [AH-23] Issue faced"."""
    if not ticket_id:
        return subject
    if extract_ticket_id(subject, project_key):
        return subject
    return f"[{ticket_id}] {subject}"


def _text_to_html(text: str) -> str:
    """Convert plain-text email bodies to simple HTML: paragraphs for
    blank-line-separated blocks and <ul><li> for "- "-prefixed lines, so
    Zoho renders lists instead of one run-on paragraph."""
    import html

    blocks: list[str] = []
    bullets: list[str] = []

    def flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li>{b}</li>" for b in bullets)
            blocks.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_bullets()
            continue
        if line.startswith("- "):
            bullets.append(html.escape(line[2:].strip()))
            continue
        flush_bullets()
        blocks.append(f"<p>{html.escape(line)}</p>")
    flush_bullets()
    return "\n".join(blocks)
