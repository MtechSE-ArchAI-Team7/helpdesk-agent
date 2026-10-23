import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langfuse import Langfuse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from helpdesk_agent.api.factory import HelpdeskWorkflow
from helpdesk_agent.config.settings import LLMConfig
from helpdesk_agent.infrastructure.llm.client import LLMError, LLMTriageClient
from helpdesk_agent.observability import tracing as observability

pytestmark = pytest.mark.unit


@pytest.fixture
def spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "get_tracer", lambda: provider.get_tracer("helpdesk_agent"))
    monkeypatch.setattr(observability, "_langfuse_client", MagicMock())
    yield exporter
    provider.shutdown()


def test_generation_records_content_and_usage(spans):
    response = MagicMock()
    response.choices[0].message.content = "answer"
    response.model = "gateway/model"
    response.usage.prompt_tokens = 12
    response.usage.completion_tokens = 4
    with observability.get_tracer().start_as_current_span("request"):
        with observability.generation("triage", "gateway/model", [{"role": "user", "content": "help"}]) as span:
            observability.record_generation_response(span, response)
    child, parent = spans.get_finished_spans()
    assert child.parent.span_id == parent.context.span_id
    assert child.context.trace_id == parent.context.trace_id
    assert child.attributes["langfuse.observation.type"] == "generation"
    assert child.attributes["langfuse.observation.output"] == '"answer"'
    assert child.attributes["gen_ai.usage.input_tokens"] == 12


def test_generation_records_error_without_usage(spans):
    with pytest.raises(ValueError):
        with observability.generation("triage", "model", []):
            raise ValueError("invalid JSON")
    span = spans.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert "gen_ai.usage.input_tokens" not in span.attributes


def test_disabled_does_not_capture_content(spans, monkeypatch):
    monkeypatch.setattr(observability, "_langfuse_client", None)
    with observability.generation("triage", "model", [{"content": "private"}]):
        pass
    assert "langfuse.observation.input" not in spans.get_finished_spans()[0].attributes


def test_filter_keeps_request_and_workflow_but_not_httpx(spans):
    with observability.get_tracer().start_as_current_span("workflow"):
        pass
    assert observability._should_export_span(spans.get_finished_spans()[0])
    span = MagicMock()
    span.attributes = {"http.target": "/ping"}
    assert not observability._should_export_span(span)
    span.attributes = {}
    span.kind = SpanKind.CLIENT
    span.instrumentation_scope.name = "opentelemetry.instrumentation.httpx"
    assert not observability._should_export_span(span)
    span.kind = SpanKind.SERVER
    span.instrumentation_scope.name = "opentelemetry.instrumentation.asgi"
    assert observability._should_export_span(span)


def test_setup_uses_shared_provider_once(monkeypatch):
    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setattr(observability, "_langfuse_client", None)
    monkeypatch.setenv("HELPDESK_AGENT_LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "private-test")
    with patch.object(observability, "Langfuse") as client, patch.object(observability.trace, "set_tracer_provider"):
        observability.setup_observability(FastAPI())
        observability.setup_observability(FastAPI())
    client.assert_called_once()
    assert isinstance(client.call_args.kwargs["tracer_provider"], TracerProvider)


def test_secret_failure_does_not_break_startup_or_log_secret(monkeypatch, caplog):
    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setattr(observability, "_langfuse_client", None)
    with patch.object(observability.LangfuseConfig, "from_env", side_effect=ValueError("private-test")):
        with patch.object(observability.trace, "set_tracer_provider"):
            observability.setup_observability(FastAPI())
    assert observability._langfuse_client is None
    assert "private-test" not in caplog.text
    assert "continuing without Langfuse" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,payload",
    [
        ("triage_email", {"summary": "Login problem", "confidence": 0.9}),
        ("assess_playwright_validation", {"decision": "not_applicable", "reason": "No URL"}),
        ("evaluate_playwright_validation", {"status": "inconclusive", "summary": "No evidence"}),
    ],
)
async def test_real_llm_methods_emit_one_generation(spans, method, payload):
    response = MagicMock()
    response.choices[0].message.content = json.dumps(payload)
    response.model = "test-model"
    response.usage = None
    guardrails = MagicMock()
    guardrails.validate_input = AsyncMock()
    guardrails.validate_output = AsyncMock()
    client = LLMTriageClient(
        LLMConfig(api_key="placeholder", model="test-model"),
        guardrails=guardrails,
    )
    client.client.chat.completions.create = AsyncMock(return_value=response)
    arguments = ["Subject", "Body"] if method != "assess_playwright_validation" else ["Subject", "Body", "Summary"]
    await getattr(client, method)(*arguments)
    recorded = spans.get_finished_spans()
    assert len(recorded) == 1
    assert recorded[0].name == f"llm.{method}"
    assert "Body" in recorded[0].attributes["langfuse.observation.input"]
    assert "gen_ai.usage.input_tokens" not in recorded[0].attributes
    client.client.chat.completions.create.assert_awaited_once()
    assert client.client.chat.completions.create.await_args.kwargs["response_format"] == {"type": "json_object"}
    await client.client.close()


@pytest.mark.asyncio
async def test_invalid_llm_json_marks_generation_failed(spans):
    response = MagicMock()
    response.choices[0].message.content = "invalid JSON"
    response.model = "test-model"
    response.usage = None
    guardrails = MagicMock()
    guardrails.validate_input = AsyncMock()
    guardrails.validate_output = AsyncMock()
    client = LLMTriageClient(LLMConfig(api_key="placeholder"), guardrails=guardrails)
    client.client.chat.completions.create = AsyncMock(return_value=response)
    with pytest.raises(LLMError, match="invalid JSON"):
        await client.triage_email("Subject", "Body")
    assert spans.get_finished_spans()[0].status.status_code == StatusCode.ERROR
    await client.client.close()


@pytest.mark.asyncio
async def test_workflow_step_spans_share_trace_and_session(spans, mock_llm_client):
    from helpdesk_agent.application.diagnosis import current_trace_id

    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    with observability.get_tracer().start_as_current_span("request") as request:
        expected_id = format(request.get_span_context().trace_id, "032x")
        assert current_trace_id() == expected_id
        await workflow.run({"case_id": "case-trace", "summary": "Login issue"})
    recorded = spans.get_finished_spans()
    names = {span.name for span in recorded}
    assert {"helpdesk.workflow", "workflow.triage", "workflow.diagnose", "workflow.create_ticket"} <= names
    assert all(format(span.context.trace_id, "032x") == expected_id for span in recorded)
    assert all(span.attributes["langfuse.session.id"] == "case-trace" for span in recorded if span.name != "request")
    root = next(span for span in recorded if span.name == "helpdesk.workflow")
    assert root.parent.span_id == request.get_span_context().span_id


@pytest.mark.asyncio
async def test_caught_workflow_failure_sets_span_status(spans, mock_llm_client):
    workflow = HelpdeskWorkflow(llm_client=mock_llm_client)
    workflow.ticket_flow.create_ticket = AsyncMock(return_value={"status": "failed", "error": "unavailable"})
    result = await workflow.run({"case_id": "case-error", "summary": "Login issue"})
    assert result.current_step == "ticket_creation_failed"
    ticket = next(span for span in spans.get_finished_spans() if span.name == "workflow.create_ticket")
    assert ticket.status.status_code == StatusCode.ERROR


def test_real_fastapi_request_parent_is_exported_and_health_is_excluded():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = FastAPI()

    @app.get("/work")
    @app.get("/ping")
    def work():
        return {"status": "ok"}

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="/healthz,/ping")
    with TestClient(app) as client:
        assert client.get("/work").status_code == 200
        previous = len(exporter.get_finished_spans())
        assert client.get("/ping").status_code == 200
        assert len(exporter.get_finished_spans()) == previous
    parent = next(span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER)
    assert observability._should_export_span(parent)
    assert all(
        not observability._should_export_span(span)
        for span in exporter.get_finished_spans()
        if span.kind != SpanKind.SERVER
    )
    provider.shutdown()


def test_real_langfuse_sdk_exports_native_generation_without_network(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    key_pair = ("public-sdk-test", "private-sdk-test")
    client = Langfuse(
        public_key=key_pair[0],
        secret_key=key_pair[1],
        tracer_provider=provider,
        span_exporter=exporter,
        should_export_span=observability._should_export_span,
        sample_rate=1.0,
    )
    monkeypatch.setattr(observability, "_langfuse_client", client)
    monkeypatch.setattr(observability, "get_tracer", lambda: provider.get_tracer("helpdesk_agent"))
    with observability.operation_span("helpdesk.workflow", "sdk-case"):
        with observability.generation("llm.triage_email", "model", [{"content": "Hello"}]):
            pass
    assert provider.force_flush(timeout_millis=5000)
    recorded = exporter.get_finished_spans()
    assert len(recorded) == 2
    assert recorded[0].attributes["langfuse.observation.type"] == "generation"
    assert recorded[0].attributes["langfuse.session.id"] == "sdk-case"
    provider.shutdown()


def test_lifespan_flushes_on_shutdown():
    from helpdesk_agent.api.app import _lifespan

    with patch("helpdesk_agent.api.app.flush_observability") as flush:
        with TestClient(FastAPI(lifespan=_lifespan)):
            flush.assert_not_called()
        flush.assert_called_once()
