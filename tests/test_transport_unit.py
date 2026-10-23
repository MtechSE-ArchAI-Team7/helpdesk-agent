from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pydantic.root_model  # noqa: F401 - initialize the SDK's Pydantic v2 root model module
import pytest
from botocore.credentials import Credentials
from botocore.exceptions import NoCredentialsError, ReadTimeoutError

from helpdesk_agent.config.settings import A2AConfig, MCPConfig
from helpdesk_agent.infrastructure.a2a.client import A2AClientManager, _extract_task_id, _try_parse_json
from helpdesk_agent.infrastructure.mcp.transport import (
    GatewayIAMAuth,
    MCPProxyAdapter,
    _content_to_dict,
    _root_exception,
    _safe_error_detail,
)

pytestmark = pytest.mark.unit


def _result(
    *,
    text: str = '{"status":"completed","action_id":"act-1","content":{"value":1}}',
    error: bool = False,
    structured: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        isError=error,
        content=[SimpleNamespace(text=text)],
        structuredContent=structured or {},
    )


def _patch_mcp_session(monkeypatch: pytest.MonkeyPatch, result: SimpleNamespace) -> list:
    calls = []

    @asynccontextmanager
    async def fake_transport(url: str, **kwargs):
        calls.append(("transport", url, kwargs))
        yield object(), object(), None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            calls.append(("initialize",))

        async def call_tool(self, name: str, arguments: dict):
            calls.append(("call", name, arguments))
            return result

    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.streamablehttp_client", fake_transport)
    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.ClientSession", lambda *_: FakeSession())
    return calls


def test_mcp_content_conversion_and_error_sanitization() -> None:
    assert _content_to_dict([]) == {}
    assert _content_to_dict([SimpleNamespace(text='{"a":1}')]) == {"a": 1}
    assert _content_to_dict([SimpleNamespace(text="[1,2]")]) == {"result": [1, 2]}
    assert _content_to_dict([SimpleNamespace(text="plain")]) == {"text": "plain"}
    assert _content_to_dict([SimpleNamespace(model_dump=lambda: {"kind": "image"})]) == {"content": [{"kind": "image"}]}

    root = RuntimeError("failed https://service.invalid/path token=secret\nnext")

    class NestedFailure(Exception):
        def __init__(self, exception: BaseException) -> None:
            self.exceptions = (exception,)

    group = NestedFailure(root)
    assert _root_exception(group) is root
    assert _safe_error_detail(root) == "failed [url] token=[redacted] next"


def test_mcp_result_mapping_handles_errors_statuses_and_atlassian() -> None:
    adapter = MCPProxyAdapter(MCPConfig())
    failure = adapter._response_from_result(_result(text="bad", error=True), "browser_navigate")
    assert (failure.status, failure.error, failure.content) == ("failed", "bad", {})
    empty_failure = adapter._response_from_result(SimpleNamespace(isError=True, content=[]), "regular_tool")
    assert empty_failure.error == "tool returned isError with no message"

    assert adapter._response_from_result(_result(text='{"status":"SUCCESS"}'), "x").status == "completed"
    assert adapter._response_from_result(
        _result(text="{}", structured={"status": {"code": 201}, "content": {"ok": True}}), "x"
    ).content == {"ok": True}
    failed_code = adapter._response_from_result(
        _result(text="{}", structured={"status": {"code": 500, "description": "unavailable"}}), "x"
    )
    assert (failed_code.status, failed_code.error) == ("failed", "unavailable")
    assert (
        adapter._response_from_result(_result(text='{"status":"completed"}'), "foo_approval_request").status == "queued"
    )
    assert adapter._response_from_result(_result(text="not-json"), "jira.create_issue", True).status == "completed"
    assert adapter._response_from_result(_result(text='{"status":"working"}'), "x").status == "working"
    status_without_message = adapter._response_from_result(
        _result(text="{}", structured={"status": {"code": 500}}), "x"
    )
    assert status_without_message.error == "server returned status {'code': 500}"


@pytest.mark.asyncio
async def test_mcp_tool_routing_translation_and_queueing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp_session(monkeypatch, _result())
    config = MCPConfig(
        base_url="http://default/mcp",
        atlassian_url="http://jira/mcp",
        zohomail_url="http://mail/mcp",
        faq_url="http://faq/mcp",
        faq_tool_prefix="kb_",
        playwright_url="http://browser/mcp",
        playwright_tool_prefix="pw_",
        jira_project_key="HD",
    )
    adapter = MCPProxyAdapter(config)

    created = await adapter.call_tool("jira.create_issue", {"summary": "VPN", "reporter": "person"})
    assert created.status == "completed"
    assert calls[-1] == (
        "call",
        "jira_create_issue",
        {"project_key": "HD", "summary": "VPN", "issue_type": "Task", "description": "\n\nReporter: person"},
    )
    assert calls[-3][1] == "http://jira/mcp"

    queued = await adapter.call_tool("zohomail.Send", {"to": "a@example.test"}, wait_for_completion=False)
    assert queued.status == "queued"
    assert calls[-1] == ("call", "Send", {"to": "a@example.test"})
    await adapter.call_tool("playwright.browser_snapshot")
    assert calls[-1] == ("call", "pw_browser_snapshot", {})
    await adapter.call_tool("faq.search", {"query": "login"})
    assert calls[-1] == ("call", "kb_Retrieve", {"retrievalQuery": {"text": "login"}})
    await adapter.call_tool("other.tool", {"value": 1}, correlation_id="corr", idempotency_key="idem", policy={"p": 1})
    assert calls[-1] == (
        "call",
        "other.tool",
        {
            "arguments": {
                "value": 1,
                "correlation_id": "corr",
                "idempotency_key": "idem",
                "policy": {"p": 1},
                "wait_for_completion": True,
            }
        },
    )


def test_mcp_atlassian_translation_defaults_and_generic_fallback() -> None:
    adapter = MCPProxyAdapter(MCPConfig(atlassian_tool_prefix="at_", jira_project_key="OPS"))
    assert adapter._translate_atlassian_call("jira.create_issue", {}) == (
        "at_jira_create_issue",
        {"project_key": "OPS", "summary": "Helpdesk request", "issue_type": "Task", "description": ""},
    )
    assert adapter._translate_atlassian_call("jira.get_issue", {"issue_key": "OPS-1"}) == (
        "at_jira_get_issue",
        {"issue_key": "OPS-1"},
    )
    assert adapter._translate_atlassian_call("jira.add_comment", {}) == (
        "at_jira_add_comment",
        {"issue_key": "", "body": ""},
    )
    assert adapter._translate_atlassian_call("jira.update_issue", {"fields": None}) == (
        "at_jira_update_issue",
        {"issue_key": "", "fields": "{}"},
    )
    assert adapter._translate_atlassian_call("confluence.search", {"query": "x"}) == (
        "at_confluence_search",
        {"query": "x"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "region", "expected_region"),
    [
        ("https://gateway.bedrock-agentcore.ap-southeast-2.amazonaws.com/mcp", None, "ap-southeast-2"),
        ("https://gateway.invalid/mcp", "us-west-2", "us-west-2"),
    ],
)
async def test_mcp_faq_iam_auth_uses_discovered_or_configured_region_and_strips_bearer(
    monkeypatch: pytest.MonkeyPatch, url: str, region: str | None, expected_region: str
) -> None:
    received = {}

    @asynccontextmanager
    async def fake_transport(endpoint: str, **kwargs):
        received.update(endpoint=endpoint, kwargs=kwargs)
        yield object(), object(), None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            return _result()

    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.streamablehttp_client", fake_transport)
    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.ClientSession", lambda *_: FakeSession())
    adapter = MCPProxyAdapter(
        MCPConfig(
            faq_url=url,
            faq_auth_mode="iam",
            faq_region=region,
            headers={"authorization": "bearer", "X-Tenant": "tenant"},
        )
    )
    result = await adapter.call_tool("faq.search", {"query": "known issue"})
    assert result.status == "completed"
    assert received["endpoint"] == url
    assert received["kwargs"]["headers"] == {"X-Tenant": "tenant"}
    auth = received["kwargs"]["auth"]
    assert isinstance(auth, GatewayIAMAuth)
    assert auth.region == expected_region


@pytest.mark.asyncio
async def test_mcp_faq_auth_validation_and_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = MCPProxyAdapter(MCPConfig(faq_auth_mode="invalid"))
    result = await adapter.call_tool("faq.search", {"query": "help"})
    assert result.status == "failed"
    assert "must be bearer or iam" in result.error

    adapter = MCPProxyAdapter(MCPConfig(faq_auth_mode="iam", faq_url="https://invalid.example/mcp"))
    result = await adapter.call_tool("faq.search", {"query": "help"})
    assert result.status == "failed"
    assert "Set HELPDESK_AGENT_MCP_FAQ_REGION" in result.error

    @asynccontextmanager
    async def failing_transport(*args, **kwargs):
        raise RuntimeError("connect https://private.example/path password=hunter2")
        yield

    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.streamablehttp_client", failing_transport)
    adapter = MCPProxyAdapter(MCPConfig())
    result = await adapter.call_tool("ordinary")
    assert result.status == "failed"
    assert result.error == "RuntimeError: connect https://private.example/path password=hunter2"

    adapter = MCPProxyAdapter(MCPConfig(playwright_tool_prefix=""))
    result = await adapter.call_tool("playwright.browser_navigate", {"url": "https://example.test"})
    assert result.status == "failed"
    assert "connect" in result.error


@pytest.mark.asyncio
async def test_mcp_sequence_rejects_mixed_urls_stops_on_failure_and_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = MCPProxyAdapter(MCPConfig(base_url="http://default/mcp", playwright_url="http://browser/mcp"))
    assert await adapter.call_tool_sequence([]) == []
    mixed = await adapter.call_tool_sequence([("x", {}), ("playwright.browser_snapshot", {})])
    assert len(mixed) == 1 and "single MCP server" in mixed[0].error

    calls = _patch_mcp_session(monkeypatch, _result(text='{"status":"failed","error":"denied"}'))
    responses = await adapter.call_tool_sequence([("first", {}), ("second", {})])
    assert len(responses) == 1
    assert responses[0].status == "failed"
    assert [call[1] for call in calls if call[0] == "call"] == ["first"]

    calls = _patch_mcp_session(monkeypatch, _result(text='{"action_id":"new","status":"ok","content":{"x":2}}'))
    polled = await adapter.get_action_status("old")
    assert (polled.action_id, polled.status, polled.content) == ("new", "ok", {"x": 2})
    assert calls[-1] == ("call", "actions/get", {"action_id": "old"})

    calls = _patch_mcp_session(monkeypatch, SimpleNamespace(isError=True, content=[SimpleNamespace(text="no")]))
    polled = await adapter.get_action_status("old")
    assert (polled.status, polled.error) == ("failed", "no")

    @asynccontextmanager
    async def broken_transport(*args, **kwargs):
        raise RuntimeError("network broke")
        yield

    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.streamablehttp_client", broken_transport)
    polled = await adapter.get_action_status("same-id")
    assert (polled.action_id, polled.status, polled.error) == ("same-id", "failed", "network broke")

    @asynccontextmanager
    async def available_transport(*args, **kwargs):
        yield object(), object(), None

    class FailingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            raise RuntimeError("tool unavailable")

    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.streamablehttp_client", available_transport)
    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.ClientSession", lambda *_: FailingSession())
    failed_sequence = await adapter.call_tool_sequence([("first", {})])
    assert len(failed_sequence) == 1
    assert failed_sequence[0].status == "failed"
    assert failed_sequence[0].error == "RuntimeError: tool unavailable"


@pytest.mark.asyncio
async def test_mcp_playwright_sequence_lists_catalog_and_translates_all_tool_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    @asynccontextmanager
    async def fake_transport(*args, **kwargs):
        yield object(), object(), None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

        async def list_tools(self, cursor=None):
            calls.append(("list", cursor))
            return SimpleNamespace(
                tools=[SimpleNamespace(name="first")],
                nextCursor="next" if cursor is None else None,
            )

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return _result()

    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.streamablehttp_client", fake_transport)
    monkeypatch.setattr("helpdesk_agent.infrastructure.mcp.transport.ClientSession", lambda *_: FakeSession())
    adapter = MCPProxyAdapter(
        MCPConfig(
            playwright_url="http://browser/mcp",
            zohomail_url="http://browser/mcp",
            atlassian_url="http://browser/mcp",
            atlassian_tool_prefix="at_",
            playwright_tool_prefix="pw_",
        )
    )
    responses = await adapter.call_tool_sequence(
        [
            ("jira.get_issue", {"issue_key": "OPS-1"}),
            ("zohomail.Send", {"to": "x"}),
            ("playwright.browser_snapshot", {}),
        ]
    )
    assert len(responses) == 3
    assert calls == [
        ("list", None),
        ("list", "next"),
        ("at_jira_get_issue", {"issue_key": "OPS-1"}),
        ("Send", {"to": "x"}),
        ("pw_browser_snapshot", {}),
    ]


@pytest.mark.asyncio
async def test_gateway_iam_auth_refreshes_credentials_and_rejects_missing_credentials() -> None:
    auth = GatewayIAMAuth("ap-southeast-2")
    auth.session = MagicMock()
    auth.session.get_credentials.return_value = Credentials("access", "secret", "session")
    request = httpx.Request("POST", "https://example.amazonaws.com/mcp", content=b"{}")
    signed = await anext(auth.async_auth_flow(request))
    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert signed.headers["X-Amz-Security-Token"] == "session"

    auth.session.get_credentials.return_value = None
    with pytest.raises(NoCredentialsError):
        auth._sign(request)


def test_a2a_parse_helpers_and_targets() -> None:
    assert _try_parse_json(SimpleNamespace(json=lambda: {"ok": True})) == {"ok": True}
    assert _try_parse_json(SimpleNamespace(json=lambda: [])) is None
    assert _try_parse_json(SimpleNamespace(json=MagicMock(side_effect=ValueError))) is None
    assert _extract_task_id(None) is None
    assert _extract_task_id({"result": []}) is None
    assert _extract_task_id({"result": {"task": {"id": "nested"}}}) == "nested"
    assert _extract_task_id({"result": {"kind": "task", "id": "direct"}}) == "direct"
    assert _extract_task_id({"result": {"task": {"id": 7}, "kind": "task", "id": 8}}) is None
    assert _extract_task_id({"result": {}}) is None

    manager = A2AClientManager(
        A2AConfig(remote_agents={"z": "http://z", "a": "http://a"}, remote_agent_runtime_arns={"runtime": "arn"})
    )
    assert manager.agent_names == ["a", "z"]
    assert manager.has_target("runtime")
    assert not manager.has_target("missing")
    assert manager._resolve_target("runtime") == ("", "")
    with pytest.raises(KeyError):
        manager._resolve_target("missing")
    with pytest.raises(KeyError):
        asyncio.run(manager.get_agent_card("missing"))


@pytest.mark.asyncio
async def test_a2a_http_handoff_payload_headers_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = A2AClientManager(
        A2AConfig(remote_agents={"remote": "http://remote///"}, remote_agent_tokens={"remote": "bearer"})
    )
    http = SimpleNamespace(
        post=AsyncMock(
            return_value=SimpleNamespace(status_code=200, json=lambda: {"result": {"task": {"id": "task-1"}}})
        )
    )
    monkeypatch.setattr(manager, "_ensure_client_stack", AsyncMock(return_value=(http, None)))
    outcome = await manager.send_case("remote", {"case_id": "case-1"})
    assert outcome.submitted and outcome.task_id == "task-1"
    url, kwargs = http.post.call_args.args[0], http.post.call_args.kwargs
    assert url == "http://remote/"
    assert kwargs["headers"] == {"A2A-Version": "1.0", "Authorization": "Bearer bearer"}
    assert kwargs["json"]["method"] == "SendMessage"
    assert kwargs["json"]["params"]["message"]["parts"] == [{"data": {"case_id": "case-1"}}]

    http.post.return_value = SimpleNamespace(status_code=503, json=lambda: {})
    rejected = await manager.send_case("remote", {})
    assert (rejected.submitted, rejected.reason) == (False, "remote returned HTTP 503")
    http.post.side_effect = httpx.ConnectError("offline")
    failed = await manager.send_case("remote", {})
    assert (failed.submitted, failed.reason) == (False, "transport error: ConnectError")


@pytest.mark.asyncio
async def test_a2a_agentcore_handoff_handles_all_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    config = A2AConfig(
        remote_agent_runtime_arns={"remote": "arn:runtime"},
        remote_agent_tokens={"remote": "secret"},
    )
    manager = A2AClientManager(config)
    envelope = {"jsonrpc": "2.0", "params": {"message": {"parts": [{"data": {"case": 1}}]}}}
    missing = await manager._send_via_agentcore("remote", "arn", envelope, "")
    assert (missing.submitted, missing.reason) == (False, "missing bearer token for AgentCore runtime")

    success = {"result": {"kind": "task", "id": "remote-task"}}
    monkeypatch.setattr("helpdesk_agent.infrastructure.a2a.client.asyncio.to_thread", AsyncMock(return_value=success))
    first = await manager._send_via_agentcore("remote", "arn", envelope, "secret")
    second = await manager._send_via_agentcore("remote", "arn", envelope, "secret")
    assert first.submitted and first.task_id == "remote-task"
    assert first.run_id == second.run_id

    monkeypatch.setattr(
        "helpdesk_agent.infrastructure.a2a.client.asyncio.to_thread",
        AsyncMock(return_value={"error": {"message": "rejected"}}),
    )
    rejected = await manager._send_via_agentcore("remote", "arn", envelope, "secret")
    assert (rejected.submitted, rejected.reason) == (False, "JSON-RPC error: rejected")

    timeout = ReadTimeoutError(endpoint_url="https://runtime")
    monkeypatch.setattr("helpdesk_agent.infrastructure.a2a.client.asyncio.to_thread", AsyncMock(side_effect=timeout))
    timed_out = await manager._send_via_agentcore("remote", "arn", envelope, "secret")
    assert timed_out.submitted and timed_out.reason == "submitted; response not awaited"

    monkeypatch.setattr(
        "helpdesk_agent.infrastructure.a2a.client.asyncio.to_thread",
        AsyncMock(side_effect=RuntimeError("boto failed")),
    )
    failed = await manager._send_via_agentcore("remote", "arn", envelope, "secret")
    assert not failed.submitted and failed.reason == "AgentCore error: RuntimeError"

    event_handlers = {}
    request_headers = {}
    fake_runtime = MagicMock()
    fake_runtime.meta.events.register.side_effect = lambda event, callback: event_handlers.update({event: callback})
    fake_runtime.invoke_agent_runtime.return_value = {"response": BytesIO(b'{"result":{"task":{"id":"invoked-task"}}}')}
    boto3_mock = MagicMock(client=MagicMock(return_value=fake_runtime))
    monkeypatch.setitem(sys.modules, "boto3", boto3_mock)

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr("helpdesk_agent.infrastructure.a2a.client.asyncio.to_thread", run_inline)
    invoked = await manager._send_via_agentcore("remote", "arn:runtime", envelope, "secret-token")
    assert invoked.submitted and invoked.task_id == "invoked-task"
    event_handlers["before-sign.bedrock-agentcore.InvokeAgentRuntime"](SimpleNamespace(headers=request_headers))
    assert request_headers == {
        "A2A-Version": "1.0",
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Authorization": "Bearer secret-token",
    }
    invocation = fake_runtime.invoke_agent_runtime.call_args.kwargs
    assert invocation["agentRuntimeArn"] == "arn:runtime"
    assert invocation["runtimeSessionId"] == invoked.run_id
    assert invocation["payload"] == json.dumps(envelope).encode()

    fake_runtime.invoke_agent_runtime.return_value = {}
    no_response = await manager._send_via_agentcore("remote", "arn:runtime", envelope, "secret-token")
    assert no_response.submitted and no_response.task_id is None
    fake_runtime.invoke_agent_runtime.return_value = {"response": BytesIO(b"not-json")}
    unreadable = await manager._send_via_agentcore("remote", "arn:runtime", envelope, "secret-token")
    assert unreadable.submitted and unreadable.task_id is None

    empty_manager = A2AClientManager(A2AConfig())
    await empty_manager.aclose()


@pytest.mark.asyncio
async def test_a2a_message_client_cache_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    from helpdesk_agent.infrastructure.a2a import client as a2a_client

    card = MagicMock()
    resolver = MagicMock()
    resolver.get_agent_card = AsyncMock(return_value=card)
    monkeypatch.setattr(a2a_client, "A2ACardResolver", lambda *_: resolver)
    manager = A2AClientManager(A2AConfig(remote_agents={"r": "http://remote"}))
    assert await manager.get_agent_card("r") is card
    assert await manager.get_agent_card("r") is card
    assert resolver.get_agent_card.await_count == 1
    assert await manager.list_agents() == {"r": card}

    remote_client = MagicMock()

    async def events(message):
        assert message.context_id == "context"
        assert message.parts[0].root.text == "hello"
        yield "event-1"
        yield "event-2"

    remote_client.send_message = events
    factory = MagicMock()
    factory.create.return_value = remote_client
    monkeypatch.setattr(manager, "_ensure_client_stack", AsyncMock(return_value=(object(), factory)))
    assert await manager.send_message("r", "hello", "context") == ["event-1", "event-2"]
    assert await manager._get_client("r") is remote_client
    factory.create.assert_called_once_with(card)

    http_client = MagicMock(aclose=AsyncMock())
    manager._httpx_client = http_client
    await manager._ensure_client_stack()
    await manager.aclose()
    http_client.aclose.assert_awaited_once()
    assert manager._httpx_client is None
    assert manager._factory is None
