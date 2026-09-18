"""Low-cardinality Prometheus metrics for the durable run worker."""

from prometheus_client import REGISTRY as _REGISTRY
from prometheus_client import Counter, Gauge, Histogram

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
