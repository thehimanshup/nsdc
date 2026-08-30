"""Optional OpenTelemetry tracing for the graph engine — Phase 6f (Layer 6).

Sovereign + off by default. Enable with OTEL_TRACING=true. Exports to an OTLP
endpoint if OTEL_EXPORTER_OTLP_ENDPOINT is set, else to the console (dev).
If opentelemetry isn't installed, every hook is a no-op — the engine still
runs. LangSmith is deliberately NOT used (US SaaS; DPDP/sovereignty).
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

log = logging.getLogger("graph.tracing")

_ENABLED = os.getenv("OTEL_TRACING", "false").lower() == "true"
_tracer = None


def init_tracing() -> bool:
    """Set up a tracer provider once. Returns True if tracing is active."""
    global _tracer
    if not _ENABLED:
        return False
    if _tracer is not None:
        return True
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (BatchSpanProcessor,
                                                    ConsoleSpanExporter)
        provider = TracerProvider(resource=Resource.create(
            {"service.name": "govt-agents-graph"}))
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter)
            exporter = OTLPSpanExporter(endpoint=endpoint)
        else:
            exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("govt.graph")
        log.info("OpenTelemetry tracing ON (%s)", endpoint or "console")
        return True
    except Exception as e:
        log.warning("OTEL requested but unavailable (%s) — tracing disabled", e)
        return False


@contextmanager
def span(name: str, **attrs):
    """Span context manager; no-op when tracing is off/unavailable."""
    if not _ENABLED or _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name) as s:
        try:
            for k, v in attrs.items():
                if v is not None:
                    s.set_attribute(k, v)
        except Exception:
            pass
        yield


def enabled() -> bool:
    return bool(_ENABLED and _tracer is not None)
