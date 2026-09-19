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

import logging
import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

logger = logging.getLogger(__name__)

_configured = False


def setup_observability(app: FastAPI) -> None:
    """Configure the tracer provider (once) and instrument the FastAPI app.

    Safe to call for every create_app() invocation; the global tracer
    provider is only installed on the first call.
    """
    global _configured

    if not _configured:
        resource = Resource.create(
            {
                "service.name": os.getenv("OTEL_SERVICE_NAME", "helpdesk-agent"),
                "service.version": os.getenv("OTEL_SERVICE_VERSION", "0.1.0"),
            }
        )
        provider = TracerProvider(resource=resource)

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
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
