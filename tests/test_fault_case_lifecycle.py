from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incidentpilot.benchmarks import load_fault_cases

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "reference" / "orders_api"
CASES = load_fault_cases(ROOT / "benchmarks" / "fault_cases")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_fault_case_setup_reproduce_reset(case) -> None:
    sys.path.insert(0, str(REF))
    try:
        from app import create_app

        with TestClient(create_app("sqlite:///:memory:")) as client:
            setup = client.post(
                "/admin/fault",
                json={"fault_type": case.fault_type, "params": case.params, "case_id": case.id},
            )
            assert setup.status_code == 200
            reproduced = client.get("/orders")
            assert reproduced.status_code in {200, 500}
            reset = client.post("/admin/fault/reset")
            assert reset.status_code == 200
            assert client.get("/health").json()["fault_type"] == ""
    finally:
        sys.path.remove(str(REF))
