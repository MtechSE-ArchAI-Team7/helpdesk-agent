from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol, TypedDict

from helpdesk_agent.domain.models.browser import PlaywrightAssessment, PlaywrightEvaluation
from helpdesk_agent.domain.models.triage import TriageResult


class ChatMessage(TypedDict):
    role: Literal["system", "user"]
    content: str


class LLMPort(Protocol):
    def generate(self, name: str, messages: list[ChatMessage]) -> AbstractAsyncContextManager[str | None]: ...


class ReasoningPort(Protocol):
    async def triage_email(
        self,
        subject: str,
        body: str,
        history_snippets: list[str] | None = None,
    ) -> TriageResult: ...

    async def assess_playwright_validation(
        self,
        subject: str,
        body: str,
        triage_summary: str,
    ) -> PlaywrightAssessment: ...

    async def evaluate_playwright_validation(self, expected_error: str, evidence: str) -> PlaywrightEvaluation: ...

    async def draft_clarification(self, triage: TriageResult, raw_text: str) -> str: ...

    async def draft_resolution(self, faq_article: dict[str, Any], raw_text: str) -> str: ...


class TicketingPort(Protocol):
    async def create_ticket(
        self,
        case_id: str,
        summary: str,
        description: str,
        service: str,
        priority: str,
        reporter: str,
    ) -> dict[str, Any]: ...

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]: ...

    async def add_comment(self, ticket_id: str, body: str) -> dict[str, Any]: ...

    async def update_ticket(
        self,
        ticket_id: str,
        *,
        summary: str | None = None,
        description: str | None = None,
        priority: str | None = None,
    ) -> dict[str, Any]: ...


class EmailPort(Protocol):
    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ) -> dict[str, Any]: ...

    async def reply_email(
        self,
        message_id: str,
        body: str,
        subject: str | None = None,
        to: str | None = None,
        html_body: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_messages(self, query: str = "is:unread", max_results: int = 10) -> dict[str, Any]: ...


class KnowledgePort(Protocol):
    async def search(self, query: str, max_results: int = 5) -> dict[str, Any]: ...


class BrowserPort(Protocol):
    def validate_target(self, target_url: str | None, report: str) -> tuple[str | None, str]: ...

    async def validate(self, target_url: str) -> dict[str, Any]: ...


class DeliveryResult(Protocol):
    @property
    def submitted(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...

    @property
    def task_id(self) -> str | None: ...

    @property
    def run_id(self) -> str | None: ...


class DiagnosisPort(Protocol):
    def has_target(self, name: str) -> bool: ...

    async def send_case(
        self,
        agent_name: str,
        case_payload: dict[str, Any],
        context_id: str | None = None,
    ) -> DeliveryResult: ...


class MemoryPort(Protocol):
    @property
    def context_chars(self) -> int: ...

    async def save_message(self, ticket_id: str, role: str, content: str, identity: str) -> None: ...

    async def history(self, ticket_id: str, incoming_identity: str) -> list[str]: ...

    async def similar(self, ticket_id: str, query: str) -> list[dict[str, Any]]: ...

    async def save_snapshot(self, ticket_id: str, snapshot: dict[str, Any], identity: str) -> None: ...

    async def conversation_ticket(self, conversation_id: str) -> str | None: ...

    async def bind_conversation(self, conversation_id: str, ticket_id: str) -> None: ...
