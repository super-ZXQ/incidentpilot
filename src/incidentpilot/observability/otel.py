"""OpenTelemetry setup: vendor-neutral tracing."""

from __future__ import annotations

import logging
from typing import Any

from incidentpilot.config import Settings, get_settings

logger = logging.getLogger(__name__)

_configured = False
_tracer = None


def configure_otel(settings: Settings | None = None) -> Any:
    global _configured, _tracer
    if _configured:
        return _tracer
    settings = settings or get_settings()
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )

        resource = Resource.create({"service.name": settings.otel_service_name})
        provider = TracerProvider(resource=resource)
        if settings.otel_console_exporter:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        if settings.otel_otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=settings.otel_otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(settings.otel_service_name)
        _configured = True
        return _tracer
    except Exception:
        logger.exception("OTel configuration failed; continuing without tracer")
        _configured = True
        return None


def get_tracer():
    if _tracer is None:
        configure_otel()
    return _tracer


class NullSpan:
    def __enter__(self) -> NullSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def record_exception(self, exc: Exception) -> None:
        return None


class _SimpleSpan:
    """Non-context-manager span that avoids anyio/contextvar detach issues."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def __enter__(self) -> _SimpleSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._span.end()

    def set_attribute(self, key: str, value: Any) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._span.set_attribute(key, value)

    def record_exception(self, exc: Exception) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._span.record_exception(exc)


def start_span(name: str, attributes: dict[str, Any] | None = None):
    tracer = get_tracer()
    if tracer is None:
        return NullSpan()
    try:
        span = tracer.start_span(name)
        wrapper = _SimpleSpan(span)
        if attributes:
            for k, v in attributes.items():
                wrapper.set_attribute(k, v)
        return wrapper
    except Exception:
        return NullSpan()
