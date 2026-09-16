"""Reference orders-api tests."""

from __future__ import annotations

import time

from app import create_app, reset_fault
from fastapi.testclient import TestClient


def test_health_and_orders() -> None:
    client = TestClient(create_app("sqlite:///:memory:"))
    assert client.get("/health").json()["status"] == "ok"
    orders = client.get("/orders").json()
    assert isinstance(orders, list)
    assert len(orders) >= 1


def test_fault_slow_query_increases_latency() -> None:
    app = create_app("sqlite:///:memory:")
    client = TestClient(app)
    reset_fault()
    baseline = client.get("/metrics").json()["p95_latency_ms"]
    client.post("/admin/fault", json={"fault_type": "slow_database_query", "params": {"delay_seconds": 0.2}})
    start = time.perf_counter()
    resp = client.get("/orders")
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert elapsed >= 0.15
    metrics = client.get("/metrics").json()
    assert metrics["fault_type"] == "slow_database_query"
    assert metrics["request_count"] >= 1
    assert baseline == 0.0 or metrics["p95_latency_ms"] >= baseline
    reset_fault()


def test_fault_null_exception_returns_500() -> None:
    client = TestClient(create_app("sqlite:///:memory:"))
    reset_fault()
    client.post("/admin/fault", json={"fault_type": "null_exception"})
    resp = client.get("/orders")
    assert resp.status_code == 500
    reset_fault()
