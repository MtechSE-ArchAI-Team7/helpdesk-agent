from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from helpdesk_agent.domain.exceptions import LLMError
from helpdesk_agent.domain.models.browser import PlaywrightAssessment, PlaywrightEvaluation
from helpdesk_agent.domain.models.triage import TriageLLMResponse, TriageResult
from helpdesk_agent.ports.capabilities import ChatMessage, LLMPort
from helpdesk_agent.prompts.browser.assessment import SYSTEM as ASSESSMENT_SYSTEM
from helpdesk_agent.prompts.browser.assessment import render as render_assess_playwright_validation
from helpdesk_agent.prompts.browser.evidence import SYSTEM as EVIDENCE_SYSTEM
from helpdesk_agent.prompts.browser.evidence import render as render_evaluate_playwright_validation
from helpdesk_agent.prompts.communication.draft_clarification import draft_clarification as render_clarification
from helpdesk_agent.prompts.communication.draft_resolution import draft_resolution as render_resolution
from helpdesk_agent.prompts.triage.email import SYSTEM as TRIAGE_SYSTEM
from helpdesk_agent.prompts.triage.email import render as render_triage_email
from helpdesk_agent.reasoning.guardrails import GuardrailsValidator


class HelpdeskReasoner:
    def __init__(self, transport: LLMPort, guardrails: GuardrailsValidator) -> None:
        self.transport = transport
        self.guardrails = guardrails

    async def _complete_json(
        self,
        name: str,
        messages: list[ChatMessage],
        empty_message: str,
        output_model: type[BaseModel],
    ) -> dict[str, Any]:
        async with self.transport.generate(name, messages) as content:
            if not content:
                raise LLMError(empty_message)
            data = json.loads(content)
            if not isinstance(data, dict):
                raise LLMError("LLM returned a non-object JSON response")
            await self.guardrails.validate_output(content, output_model)
            return data

    async def triage_email(
        self,
        subject: str,
        body: str,
        history_snippets: list[str] | None = None,
    ) -> TriageResult:
        """
        Analyze an email and return structured triage results.

        Args:
            subject: Email subject line
            body: Email body text
            history_snippets: Optional list of previous email snippets for context

        Returns:
            TriageResult with service, issue_type, priority, confidence, etc.

        Raises:
            LLMError: If the LLM call fails or returns invalid JSON
        """
        prompt = render_triage_email(subject, body, history_snippets)

        try:
            await self.guardrails.validate_input(
                {
                    "subject": subject,
                    "body": body,
                    **{f"history_{index}": snippet for index, snippet in enumerate(history_snippets or [])},
                }
            )
            data = await self._complete_json(
                "llm.triage_email",
                messages=[
                    {
                        "role": "system",
                        "content": (TRIAGE_SYSTEM),
                    },
                    {"role": "user", "content": prompt},
                ],
                empty_message="LLM returned empty response",
                output_model=TriageLLMResponse,
            )

            return TriageResult(
                service=data.get("service", "unknown"),
                issue_type=data.get("issue_type", "general"),
                priority=data.get("priority", "low"),
                confidence=float(data.get("confidence", 0.5)),
                missing_info=data.get("missing_info", []),
                clarifying_questions=data.get("clarifying_questions", []),
                summary=data.get("summary", subject[:100]),
                source="email",
                sender="unknown",
                raw_text=f"{subject}\n\n{body}",
            )

        except json.JSONDecodeError as e:
            raise LLMError(f"LLM returned invalid JSON: {e}") from e
        except Exception as e:
            raise LLMError(f"LLM call failed: {e}") from e

    async def assess_playwright_validation(
        self,
        subject: str,
        body: str,
        triage_summary: str,
    ) -> PlaywrightAssessment:
        """Decide whether a reported problem can be safely checked in a browser."""
        prompt = render_assess_playwright_validation(subject, body, triage_summary)

        try:
            await self.guardrails.validate_input({"subject": subject, "body": body, "triage_summary": triage_summary})
            data = await self._complete_json(
                "llm.assess_playwright_validation",
                messages=[
                    {
                        "role": "system",
                        "content": (ASSESSMENT_SYSTEM),
                    },
                    {"role": "user", "content": prompt},
                ],
                empty_message="LLM returned empty Playwright assessment",
                output_model=PlaywrightAssessment,
            )
            return PlaywrightAssessment.model_validate(data)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM returned invalid Playwright assessment JSON: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"Playwright feasibility assessment failed: {exc}") from exc

    async def evaluate_playwright_validation(
        self,
        expected_error: str,
        evidence: str,
    ) -> PlaywrightEvaluation:
        """Compare browser observations with the reported symptom."""
        prompt = render_evaluate_playwright_validation(expected_error, evidence)

        try:
            await self.guardrails.validate_input({"expected_error": expected_error, "evidence": evidence})
            data = await self._complete_json(
                "llm.evaluate_playwright_validation",
                messages=[
                    {
                        "role": "system",
                        "content": EVIDENCE_SYSTEM,
                    },
                    {"role": "user", "content": prompt},
                ],
                empty_message="LLM returned empty Playwright evaluation",
                output_model=PlaywrightEvaluation,
            )
            return PlaywrightEvaluation.model_validate(data)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM returned invalid Playwright evaluation JSON: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"Playwright evidence evaluation failed: {exc}") from exc

    async def draft_clarification(self, triage: TriageResult, raw_text: str) -> str:
        return render_clarification(triage, raw_text)

    async def draft_resolution(self, faq_article: dict[str, Any], raw_text: str) -> str:
        return render_resolution(faq_article, raw_text)
