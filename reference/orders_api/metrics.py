"""Real Prometheus metrics for reference orders-api."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUEST_COUNT = Counter(
    "orders_api_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)
REQUEST_ERROR_COUNT = Counter(
    "orders_api_request_errors_total",
    "Total HTTP errors",
    ["endpoint", "error_type"],
)
REQUEST_DURATION = Histogram(
    "orders_api_request_duration_seconds",
    "Request latency",
    ["endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
DB_QUERY_DURATION = Histogram(
    "orders_api_db_query_duration_seconds",
    "DB query latency",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0),
)
INFLIGHT = Gauge("orders_api_inflight_requests", "In-flight requests")


def observe_request(endpoint: str, method: str, status: int, duration_s: float) -> None:
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=str(status)).inc()
    REQUEST_DURATION.labels(endpoint=endpoint).observe(duration_s)
    if status >= 400:
        REQUEST_ERROR_COUNT.labels(endpoint=endpoint, error_type=f"http_{status}").inc()


def observe_db(operation: str, duration_s: float) -> None:
    DB_QUERY_DURATION.labels(operation=operation).observe(duration_s)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
