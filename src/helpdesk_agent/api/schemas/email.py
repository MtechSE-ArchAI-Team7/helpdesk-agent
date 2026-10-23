from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, model_validator


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text extractor using only the stdlib."""

    _BLOCK_TAGS = {"br", "div", "p", "tr", "li"}
    _SKIP_TAGS = {"style", "script"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        text = "".join(self._chunks)
        text = re.sub(r"[ \t]+", " ", text)
        # Preserve paragraph breaks, then unwrap lone newlines (visual wraps) into spaces.
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
        return text.strip()


def _strip_html(html: str) -> str:
    """Strip tags from an HTML fragment and return readable plain text."""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def _epoch_ms_to_iso(value: int) -> str:
    """Convert epoch milliseconds to an ISO 8601 UTC string."""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


class EmailWebhook(BaseModel):
    """Incoming email webhook payload.

    Accepts the canonical field names, common email-automation tool field
    names (e.g. Zapier/n8n/Make Gmail triggers send from_email, body_plain,
    message_url, from_name, date), and Zoho Mail webhook payloads
    (fromAddress, summary, html, messageId, sentDateInGMT, ...).
    """

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _normalize_zoho(cls, data: Any) -> Any:
        """Normalize a Zoho Mail webhook payload to the canonical field names."""
        if not isinstance(data, dict) or "fromAddress" not in data:
            return data

        normalized = dict(data)

        # Zoho `sender` is the display name; the address is in `fromAddress`.
        normalized["sender"] = data.get("fromAddress")
        if data.get("sender") and not data.get("from_name"):
            normalized["from_name"] = data["sender"]

        # Body: prefer the full html (stripped to text); Zoho's `summary` is an
        # auto-generated truncated preview, so only use it as a fallback.
        if data.get("html"):
            normalized["body"] = _strip_html(data["html"])
        elif (data.get("summary") or "").strip():
            normalized["body"] = data["summary"].strip()

        if data.get("messageId") is not None:
            normalized["message_id"] = str(data["messageId"])

        sent = data.get("sentDateInGMT") or data.get("receivedTime")
        if isinstance(sent, (int, float)):
            normalized["date"] = _epoch_ms_to_iso(int(sent))

        # Remaining Zoho fields, snake_cased.
        if data.get("toAddress"):
            normalized["to_address"] = data["toAddress"]
        if data.get("ccAddress"):
            normalized["cc_address"] = data["ccAddress"]
        if data.get("folderId") is not None:
            normalized["folder_id"] = data["folderId"]
        if data.get("zuid") is not None:
            normalized["zuid"] = data["zuid"]
        if data.get("size") is not None:
            normalized["size"] = data["size"]
        if data.get("IntegIdList"):
            normalized["integ_id_list"] = data["IntegIdList"]

        return normalized

    sender: str = Field(
        ...,
        validation_alias=AliasChoices("sender", "from_email"),
        description="The sender email address",
    )
    subject: str = Field(..., description="The email subject")
    body: str = Field(
        ...,
        validation_alias=AliasChoices("body", "body_plain"),
        description="The email body text",
    )
    message_id: str | None = Field(
        None,
        validation_alias=AliasChoices("message_id", "message_url"),
        description="Email Message-ID header or message URL",
    )
    in_reply_to: str | None = Field(None, description="Email In-Reply-To header")
    references: list[str] | None = Field(None, description="Email References header values")
    attachments: list[dict[str, str]] | None = Field(None, description="Attachment metadata")
    from_name: str | None = Field(None, description="Sender display name")
    date: str | None = Field(None, description="Email date header")
    to_address: str | None = Field(None, description="Recipient address(es)")
    cc_address: str | None = Field(None, description="CC address(es)")
    folder_id: int | None = Field(None, description="Zoho folder ID")
    zuid: int | None = Field(None, description="Zoho user ID")
    size: int | None = Field(None, description="Email size in bytes")
    integ_id_list: str | None = Field(None, description="Zoho integration ID list")


class WebhookResponse(BaseModel):
    case_id: str
    run_id: str
    summary: str
    service: str
    current_step: str
    messages: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    ticket: dict[str, Any] | None
    metadata: dict[str, Any]
    history: list[dict[str, Any]]
    triage: dict[str, Any] | None = None
    faq_matches: list[dict[str, Any]] = Field(default_factory=list)
    clarification: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
