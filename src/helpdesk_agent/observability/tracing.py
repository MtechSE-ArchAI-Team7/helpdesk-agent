"""OpenTelemetry observability setup (distributed tracing).

Tracing is always on. When an OTLP endpoint is configured, spans are
exported via OTLP/HTTP (batch processor) to a collector (Jaeger, Tempo,
Azure Monitor, etc.). Otherwise no exporter is installed — spans are still
created (so trace/span ids exist for log correlation) but nothing is
written, keeping stdout/CloudWatch clean. Set OTEL_CONSOLE_EXPORTER=1 to
dump spans to stdout (local debugging only — do NOT use in deployment,
every span lands in the app log stream).

Standard OTEL_* environment variables are honored:
  - OTEL_SERVICE_NAME            (default: helpdesk-agent)
  - OTEL_EXPORTER_OTLP_ENDPOINT  (e.g. http://localhost:4318)
  - OTEL_EXPORTER_OTLP_HEADERS   (e.g. authorization=Bearer ...)
  - OTEL_CONSOLE_EXPORTER        (set to 1/true for local console span dumps)

Instrumented surfaces:
  - FastAPI inbound requests (all routers incl. /webhooks/email, /chat, /a2a)
  - httpx outbound calls (OpenAI SDK, MCP proxy adapters)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from langfuse import Langfuse
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from helpdesk_agent.config.settings import LangfuseConfig

if TYPE_CHECKING:
    from helpdesk_agent.domain.models.workflow import WorkflowState

logger = logging.getLogger(__name__)

_configured = False
_langfuse_client: Langfuse | None = None
_session_id: ContextVar[str | None] = ContextVar("helpdesk_trace_session", default=None)


def _should_export_span(span: ReadableSpan) -> bool:
    attributes = span.attributes or {}
    path = str(attributes.get("url.path", attributes.get("http.target", "")))
    if path.split("?")[0] in ("/healthz", "/ping"):
        return False
    scope = span.instrumentation_scope
    return bool(
        (scope and scope.name == "helpdesk_agent")
        or (
            span.kind == SpanKind.SERVER
            and scope
            and scope.name in ("opentelemetry.instrumentation.asgi", "opentelemetry.instrumentation.fastapi")
        )
        or (scope and scope.name == "langfuse-sdk")
    )


def flush_observability() -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush(timeout_millis=5000)


def capture_content(span: trace.Span, direction: str, value: Any) -> None:
    if _langfuse_client is not None and span.is_recording():
        span.set_attribute(f"langfuse.observation.{direction}", json.dumps(value, default=str))


def emit_workflow_event(
    state: WorkflowState,
    *,
    event_type: str,
    stage: str,
    actor_type: str,
    status: str,
    summary: str,
    duration_ms: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    span_context = trace.get_current_span().get_span_context()
    trace_id = format(span_context.trace_id, "032x") if span_context.trace_id else None
    event = state.record_event(
        event_type=event_type,
        stage=stage,
        actor_type=actor_type,
        status=status,
        summary=summary,
        trace_id=trace_id,
        duration_ms=duration_ms,
        details=details,
    )
    logger.info("agent_event=%s", json.dumps(event, separators=(",", ":"), sort_keys=True))


@contextmanager
def operation_span(name: str, session_id: str | None = None) -> Iterator[trace.Span]:
    token = _session_id.set(session_id or _session_id.get())
    try:
        with get_tracer().start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
            if _session_id.get():
                span.set_attribute("langfuse.session.id", str(_session_id.get()))
            try:
                yield span
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
    finally:
        _session_id.reset(token)


@contextmanager
def generation(name: str, model: str, messages: Any) -> Iterator[trace.Span]:
    with operation_span(name) as span:
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.request.temperature", 0.1)
        span.set_attribute("langfuse.observation.model.parameters", json.dumps({"response_format": "json_object"}))
        capture_content(span, "input", messages)
        yield span


def record_generation_response(span: trace.Span, response: Any) -> None:
    capture_content(span, "output", response.choices[0].message.content)
    usage = response.usage
    if usage is not None:
        span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
    if response.model:
        span.set_attribute("gen_ai.response.model", response.model)


def setup_observability(app: FastAPI) -> None:
    """Configure the tracer provider (once) and instrument the FastAPI app.

    Safe to call for every create_app() invocation; the global tracer
    provider is only installed on the first call.
    """
    global _configured, _langfuse_client

    if not _configured:
        resource = Resource.create(
            {
                "service.name": os.getenv("OTEL_SERVICE_NAME", "helpdesk-agent"),
                "service.version": os.getenv("OTEL_SERVICE_VERSION", "0.1.0"),
            }
        )
        provider = TracerProvider(resource=resource)

        try:
            config = LangfuseConfig.from_env()
            if config.enabled:
                destinations = (
                    os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                    os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""),
                )
                if any(destination.startswith(config.base_url + "/api/public/otel") for destination in destinations):
                    raise ValueError("Duplicate Langfuse export destination")
                _langfuse_client = Langfuse(
                    public_key=config.public_key,
                    secret_key=config.secret_key,
                    base_url=config.base_url,
                    tracer_provider=provider,
                    should_export_span=_should_export_span,
                    timeout=5,
                )
                logger.info("Langfuse tracing enabled")
        except Exception:  # noqa: BLE001
            _langfuse_client = None
            logger.warning("Langfuse initialization failed; continuing without Langfuse")

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            # OTLPSpanExporter reads OTEL_EXPORTER_OTLP_* env vars itself.
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            exporter_desc = f"otlp/http -> {endpoint}"
        elif os.getenv("OTEL_CONSOLE_EXPORTER", "").lower() in ("1", "true", "yes"):
            # Local debugging only: every span is dumped to stdout and would
            # pollute the app log stream (e.g. CloudWatch) in a deployment.
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            exporter_desc = "console"
        else:
            exporter_desc = "none (spans created but not exported)"

        trace.set_tracer_provider(provider)

        # Outbound calls made through httpx (OpenAI SDK, MCP adapters).
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except ImportError:
            logger.debug("opentelemetry-instrumentation-httpx not installed; skipping httpx tracing")

        _configured = True
        logger.info("OpenTelemetry tracing enabled (exporter: %s)", exporter_desc)

    # Per-app instrumentation; health probes excluded to reduce noise.
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="/healthz,/ping")


def get_tracer(name: str = "helpdesk_agent") -> trace.Tracer:
    """Get a tracer for creating custom spans in workflow/module code."""
    return trace.get_tracer(name)


def current_trace_id() -> str | None:
    """Return the current OTEL trace id as hex, or None when unsampled/unavailable."""
    try:
        from opentelemetry.trace import get_current_span
    except ImportError:
        return None
    span_context = get_current_span().get_span_context()
    if span_context.trace_id == 0:
        return None
    return format(span_context.trace_id, "032x")
