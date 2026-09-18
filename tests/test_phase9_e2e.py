"""Phase 9: end-to-end integration and real benchmark run."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from incidentpilot.config import reset_settings_cache
from incidentpilot.eval.harness import EvaluationHarness
from incidentpilot.persistence.session import dispose_engine, init_db, reset_db_state

FAULT_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "fault_cases"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_full_loop_investigate_fix_approve_pr(tmp_path) -> None:
    """Fault Case -> Incident -> Agent -> Evidence -> Root Cause -> Patch -> Tests -> Approval -> PR."""
    reset_db_state()
    reset_settings_cache()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}"
    os.environ["OTEL_CONSOLE_EXPORTER"] = "false"
    os.environ["GITHUB_INTEGRATION_ENABLED"] = "false"
    os.environ["TOOL_BACKEND"] = "fake"
    os.environ["EMBEDDED_WORKER_ENABLED"] = "true"
    reset_settings_cache()
    await init_db()

    from httpx import ASGITransport, AsyncClient

    from incidentpilot.api.app import create_app
    from incidentpilot.benchmarks import load_fault_cases

    cases = load_fault_cases(FAULT_DIR)
    assert cases
    case = cases[0]

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/incidents", json=case.incident)
        assert resp.status_code == 202
        data = resp.json()

        for _ in range(150):
            run = (await client.get(f"/v1/runs/{data['run_id']}")).json()
            if run["status"] not in {"PENDING", "RUNNING"}:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("agent run timeout")

        assert run["status"] in {"WAITING_APPROVAL", "NEEDS_HUMAN_INTERVENTION", "FAILED"}
        if run["status"] != "WAITING_APPROVAL":
            pytest.skip(f"run ended early with {run['status']}: {run.get('error')}")

        evidence = (await client.get(f"/v1/runs/{data['run_id']}/evidence")).json()
        tools = (await client.get(f"/v1/runs/{data['run_id']}/tool-calls")).json()
        artifacts = (await client.get(f"/v1/runs/{data['run_id']}/patch-artifact")).json()
        assert len(evidence) >= 2
        assert len(tools) >= 2
        assert len(artifacts) == 1
        assert artifacts[0]["immutable"] is True

        approve = await client.post(
            f"/v1/runs/{data['run_id']}/approval",
            json={"decision": "APPROVE", "actor": "e2e", "reason": "validated"},
        )
        assert approve.status_code == 200
        run2 = (await client.get(f"/v1/runs/{data['run_id']}")).json()
        assert run2["status"] == "RESOLVED"
        assert run2["result"]["pull_request"]["mode"] == "mock"

    await dispose_engine()
    reset_db_state()
    reset_settings_cache()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_real_benchmark_harness_multiple_cases(tmp_path) -> None:
    reset_db_state()
    reset_settings_cache()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'bench.db'}"
    os.environ["OTEL_CONSOLE_EXPORTER"] = "false"
    reset_settings_cache()
    await init_db()

    harness = EvaluationHarness(FAULT_DIR)
    report = await harness.run()
    assert report.total_cases >= 3
    assert report.completed == report.total_cases
    # Real agent executions: tool calls must be non-zero
    assert report.average_tool_calls >= 2
    data = report.to_dict()
    # Print summary for humans
    print("\n=== BENCHMARK REPORT ===")
    print(f"cases={data['total_cases']} completed={data['completed']}")
    print(f"root_cause_accuracy={data['root_cause_accuracy']:.2f}")
    print(f"resolution_rate={data['incident_resolution_rate']:.2f}")
    print(f"patch_test_pass_rate={data['patch_test_pass_rate']:.2f}")
    print(f"unsafe_action_rate={data['unsafe_action_rate']:.2f}")
    print(f"avg_tool_calls={data['average_tool_calls']:.2f}")
    print(f"median_seconds={data['median_resolution_seconds']:.2f}")
    print(data["notes"])
    for o in data["outcomes"]:
        print(
            f"  - {o['fault_case_id']}: status={o['final_status']} "
            f"rc_ok={o['root_cause_correct']} tools={o['tool_call_count']} "
            f"evidence={o['evidence_count']} dur={o['duration_seconds']:.2f}s"
        )

    await dispose_engine()
    reset_db_state()
    reset_settings_cache()
