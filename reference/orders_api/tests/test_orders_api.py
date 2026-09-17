"""Reference orders-api tests."""

from __future__ import annotations

import time

from app import create_app
from fastapi.testclient import TestClient
from faults import reset_fault


def test_health_and_orders() -> None:
    client = TestClient(create_app("sqlite:///:memory:"))
    assert client.get("/health").json()["status"] == "ok"
    orders = client.get("/orders").json()
    assert isinstance(orders, list)
    assert len(orders) >= 1


def test_prometheus_metrics_endpoint() -> None:
    client = TestClient(create_app("sqlite:///:memory:"))
    client.get("/orders")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "orders_api_requests_total" in resp.text
    meta = client.get("/metrics/json").json()
    assert meta["service"] == "orders-api"


def test_fault_slow_query_increases_latency() -> None:
    app = create_app("sqlite:///:memory:")
    client = TestClient(app)
    reset_fault()
    client.post("/admin/fault", json={"fault_type": "slow_database_query", "params": {"delay_seconds": 0.2}})
    start = time.perf_counter()
    resp = client.get("/orders")
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert elapsed >= 0.15
    meta = client.get("/metrics/json").json()
    duration_samples = meta["metrics"]["orders_api_request_duration_seconds_sum"]
    assert any(
        sample["labels"].get("endpoint") == "/orders" and sample["value"] >= 0.15
        for sample in duration_samples
    )
    assert "fault_type" not in meta
    reset_fault()


def test_fault_null_exception_returns_500() -> None:
    client = TestClient(create_app("sqlite:///:memory:"))
    reset_fault()
    client.post("/admin/fault", json={"fault_type": "null_exception"})
    resp = client.get("/orders")
    assert resp.status_code == 500
    reset_fault()
