"""Observability package."""

from incidentpilot.observability.otel import configure_otel, get_tracer, start_span

__all__ = ["configure_otel", "get_tracer", "start_span"]
