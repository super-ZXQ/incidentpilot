"""Phase 3 tests: reference orders-api + fault case loading."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from incidentpilot.benchmarks import REGISTERED_FAULT_TYPES, load_fault_case, load_fault_cases

REF_DIR = Path(__file__).resolve().parents[1] / "reference" / "orders_api"
FAULT_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "fault_cases"


def test_load_first_fault_case() -> None:
    cases = load_fault_cases(FAULT_DIR)
    assert len(cases) >= 1
    case = cases[0]
    assert case.id.startswith("fault_")
    assert case.fault_type in REGISTERED_FAULT_TYPES
    assert case.ground_truth.get("root_cause")
    assert "incident" in case.model_dump()


def test_fault_case_rejects_unknown_type(tmp_path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: x\n"
        "title: t\n"
        "category: c\n"
        "fault_type: arbitrary_shell_rm\n"
        "params: {}\n"
        "incident: {}\n"
        "ground_truth: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not registered"):
        load_fault_case(bad)


def test_reference_orders_api_fault_injection() -> None:
    sys.path.insert(0, str(REF_DIR))
    try:
        from app import create_app, reset_fault
        from fastapi.testclient import TestClient

        client = TestClient(create_app("sqlite:///:memory:"))
        reset_fault()
        assert client.get("/health").status_code == 200

        # apply n+1 fault
        resp = client.post(
            "/admin/fault",
            json={"fault_type": "n_plus_one_query", "params": {"per_row_delay": 0.02}},
        )
        assert resp.status_code == 200
        orders = client.get("/orders")
        assert orders.status_code == 200
        # Prometheus text exposition format
        metrics_text = client.get("/metrics").text
        assert "orders_api_requests_total" in metrics_text
        meta = client.get("/metrics/json").json()
        assert "orders_api_request_duration_seconds_count" in meta["metrics"]
        assert "fault_type" not in meta
        reset_fault()
    finally:
        sys.path.remove(str(REF_DIR))
