from __future__ import annotations

import logging
import re
import threading
from functools import lru_cache
from typing import TypeVar

from guardrails import Guard
from guardrails.settings import settings
from pydantic import BaseModel

logger = logging.getLogger(__name__)

OutputModel = TypeVar("OutputModel", bound=BaseModel)
_PII_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)"),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*[^\s,;]+", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b"),
)
_PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|disregard|forget|override)\b.{0,80}\b(?:previous|prior|above|system|developer)?\s*"
        r"(?:instructions?|prompts?|rules?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:reveal|print|repeat|show)\b.{0,50}\b(?:system|developer)\s+(?:prompt|instructions?)\b",
        re.I | re.S,
    ),
    re.compile(r"\b(?:act as|you are now)\s+(?:DAN|an? unrestricted assistant)\b", re.IGNORECASE),
)
_TOXIC_OUTPUT_PATTERNS = tuple(
    re.compile(rf"\b{word}\b", re.IGNORECASE) for word in ("idiot", "stupid", "moron", "useless")
) + (re.compile(r"\bshut\s+up\b", re.IGNORECASE),)


class GuardrailsRejected(Exception):
    """Raised when Guardrails cannot validate untrusted input or model output."""


class GuardrailsValidator:
    """Apply lightweight local checks and Guardrails schema validation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._schema_guards: dict[type[BaseModel], Guard] = {}
        settings.rc.enable_metrics = False
        settings.disable_tracing = True

    async def validate_input(self, values: dict[str, str]) -> None:
        self._validate_input(values)

    async def validate_output(self, content: str, output_model: type[OutputModel]) -> None:
        self._validate_output(content, output_model)

    def _validate_input(self, values: dict[str, str]) -> None:
        for field, value in values.items():
            if _contains_match(_PII_PATTERNS, value):
                logger.warning("Guardrails detected a common PII pattern in LLM input field %s", field)
            if _contains_match(_PROMPT_INJECTION_PATTERNS, value):
                raise GuardrailsRejected("Input matched a prompt-injection pattern")

    def _validate_output(self, content: str, output_model: type[OutputModel]) -> None:
        try:
            with self._lock:
                schema_guard = self._schema_guards.get(output_model)
                if schema_guard is None:
                    schema_guard = Guard.for_pydantic(output_class=output_model)
                    schema_guard.configure(allow_metrics_collection=False)
                    settings.rc.enable_metrics = False
                    self._schema_guards[output_model] = schema_guard

                schema_result = schema_guard.parse(llm_output=content, num_reasks=0)
                if not schema_result.validation_passed:
                    raise GuardrailsRejected("Model output did not match its schema")
            if _contains_match(_PII_PATTERNS, content):
                raise GuardrailsRejected("Model output matched a common PII pattern")
            if _contains_match(_TOXIC_OUTPUT_PATTERNS, content):
                raise GuardrailsRejected("Model output matched a toxic-language pattern")
        except GuardrailsRejected:
            raise
        except Exception:  # noqa: BLE001
            raise GuardrailsRejected("Model output failed Guardrails validation") from None


def _contains_match(patterns: tuple[re.Pattern[str], ...], value: str) -> bool:
    return any(pattern.search(value) for pattern in patterns)


@lru_cache(maxsize=1)
def get_guardrails_validator() -> GuardrailsValidator:
    return GuardrailsValidator()
