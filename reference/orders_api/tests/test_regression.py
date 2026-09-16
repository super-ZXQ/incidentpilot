"""Healthy-path regression tests used after a candidate fix is applied.

These tests validate correct behavior without re-injecting faults.
"""

from __future__ import annotations

from app import create_app
from fastapi.testclient import TestClient
from faults import reset_fault


def test_orders_list_returns_ok_after_fix() -> None:
    client = TestClient(create_app("sqlite:///:memory:"))
    reset_fault()
    resp = client.get("/orders")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    assert "items" in body[0]


def test_metrics_scrapeable() -> None:
    client = TestClient(create_app("sqlite:///:memory:"))
    client.get("/orders")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert b"orders_api_requests_total" in resp.content


def test_health_ok() -> None:
    client = TestClient(create_app("sqlite:///:memory:"))
    assert client.get("/health").json()["status"] == "ok"
