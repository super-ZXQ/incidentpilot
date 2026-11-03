"""Low-cardinality Prometheus metrics for the durable run worker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import REGISTRY as _REGISTRY
from prometheus_client import Counter, Gauge, Histogram

if TYPE_CHECKING:
    from threading import Thread
    from wsgiref.simple_server import WSGIServer

    from incidentpilot.config import Settings

PROMETHEUS_REGISTRY = _REGISTRY

QUEUE_DEPTH = Gauge("incidentpilot_queue_depth", "Pending durable runs")
ACTIVE_RUNS = Gauge("incidentpilot_active_runs", "Runs currently owned by this worker")
RUN_DURATION = Histogram("incidentpilot_run_duration_seconds", "Agent run duration")
RUN_RETRIES = Counter("incidentpilot_run_retries_total", "Durable run retries")
RUN_RECOVERIES = Counter("incidentpilot_run_recoveries_total", "Lease-based run recoveries")
LEASE_EXPIRATIONS = Counter("incidentpilot_lease_expirations_total", "Expired run leases")
TOOL_FAILURES = Counter(
    "incidentpilot_tool_failures_total",
    "Tool failures by bounded category",
    ("tool", "error_category"),
)
DUPLICATE_SIDE_EFFECTS = Counter(
    "incidentpilot_duplicate_side_effect_prevented_total",
    "Duplicate external side effects prevented",
)
RUNS = Counter("incidentpilot_runs_total", "Completed runs by status", ("status",))


def start_metrics_exporter(settings: Settings) -> tuple[WSGIServer, Thread] | None:
    """Serve the registry of *this* process, or bind nothing at all.

    `QUEUE_DEPTH`, `ACTIVE_RUNS` and the lease counters are only ever set inside the
    durable worker, so scraping the API instead reports the API process's permanently
    zero copies of them. Port 0 means no listener, which keeps a default install from
    opening a port for an unauthenticated endpoint.
    """
    if not settings.worker_metrics_port:
        return None
    from prometheus_client import start_http_server

    return start_http_server(
        settings.worker_metrics_port,
        addr=settings.worker_metrics_address,
        registry=PROMETHEUS_REGISTRY,
    )
