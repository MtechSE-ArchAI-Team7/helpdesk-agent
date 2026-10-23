from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pydantic.root_model  # noqa: F401 - initialize the SDK's Pydantic v2 root model module
import pytest

from helpdesk_agent.config.settings import MCPConfig
from helpdesk_agent.infrastructure.mcp.browser import PlaywrightValidationFlow
from helpdesk_agent.infrastructure.mcp.transport import MCPResponse

pytestmark = pytest.mark.unit


def _flow(hosts: list[str] | None = None) -> PlaywrightValidationFlow:
    return PlaywrightValidationFlow(config=MCPConfig(playwright_allowed_hosts=hosts or ["portal.example.com"]))


def test_reported_urls_deduplicates_and_strips_trailing_punctuation() -> None:
    report = (
        "Login failed at https://portal.example.com/account). "
        "Retry https://portal.example.com/account, or use http://help.example.test."
    )
    assert PlaywrightValidationFlow.reported_urls(report) == [
        "https://portal.example.com/account",
        "http://help.example.test",
    ]
    assert PlaywrightValidationFlow.reported_urls("no address here") == []


@pytest.mark.parametrize(
    ("target", "report", "expected"),
    [
        (None, "https://portal.example.com", "no_target_url"),
        ("https://portal.example.com", "no url given", "target_url_not_in_user_report"),
        ("https://portal.example.com:bad", "https://portal.example.com:bad", "invalid_target_url"),
        ("ftp://portal.example.com", "ftp://portal.example.com", "target_url_not_in_user_report"),
        ("https://user:pass@portal.example.com", "https://user:pass@portal.example.com", "http_or_https_url_required"),
        ("https://portal.example.com:8443", "https://portal.example.com:8443", "nonstandard_port_not_allowed"),
        ("https://unknown.example.com", "https://unknown.example.com", "host_not_allowlisted"),
    ],
)
def test_validate_target_denies_unsafe_urls(target: str | None, report: str, expected: str) -> None:
    assert _flow().validate_target(target, report) == (None, expected)


@pytest.mark.parametrize(
    ("target", "host"),
    [
        ("https://PORTAL.EXAMPLE.COM./login", "portal.example.com"),
        ("http://portal.example.com:80", "portal.example.com"),
        ("https://portal.example.com:443", "portal.example.com"),
    ],
)
def test_validate_target_allows_reported_allowlisted_host(target: str, host: str) -> None:
    assert _flow(["PORTAL.EXAMPLE.COM."]).validate_target(target, target) == (host, "allowed")


@pytest.mark.asyncio
async def test_validate_without_mcp_returns_explicit_failure() -> None:
    result = await PlaywrightValidationFlow(config=MCPConfig()).validate("https://portal.example.com")
    assert result == {"status": "failed", "tool_statuses": [], "evidence": "", "error": "mcp_disabled"}


@pytest.mark.asyncio
async def test_validate_calls_ordered_diagnostics_and_collects_error_evidence() -> None:
    adapter = SimpleNamespace(
        call_tool_sequence=AsyncMock(
            return_value=[
                MCPResponse(None, "completed", {"title": "Sign in"}),
                MCPResponse(None, "completed", {"snapshot": "button"}),
                MCPResponse(None, "failed", {}, "console exception"),
                MCPResponse(None, "completed", {"requests": []}),
            ]
        )
    )
    config = MCPConfig(playwright_timeout_seconds=17)
    result = await PlaywrightValidationFlow(adapter, config).validate("https://portal.example.com")
    args, kwargs = adapter.call_tool_sequence.call_args
    assert args[0] == [
        ("playwright.browser_navigate", {"url": "https://portal.example.com"}),
        ("playwright.browser_snapshot", {}),
        ("playwright.browser_console_messages", {"level": "error"}),
        ("playwright.browser_network_requests", {"static": False}),
    ]
    assert kwargs == {"timeout_seconds": 17}
    assert result["status"] == "failed"
    assert result["error"] == "console exception"
    assert [item["status"] for item in result["tool_statuses"]] == [
        "completed",
        "completed",
        "failed",
        "completed",
    ]
    assert "[browser_console_messages error] console exception" in result["evidence"]


@pytest.mark.asyncio
async def test_validate_marks_short_sequences_failed_and_caps_evidence() -> None:
    adapter = SimpleNamespace(
        call_tool_sequence=AsyncMock(return_value=[MCPResponse(None, "completed", {"payload": "x" * 15000})])
    )
    result = await PlaywrightValidationFlow(adapter, MCPConfig()).validate("https://portal.example.com")
    assert result["status"] == "failed"
    assert len(result["evidence"]) == PlaywrightValidationFlow._MAX_EVIDENCE_CHARS
    assert result["error"] is None
