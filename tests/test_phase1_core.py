"""Phase 1 tests: models, config, persistence, minimal API."""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from incidentpilot.config import Settings, reset_settings_cache
from incidentpilot.models.enums import AgentRunStatus, WorkflowState
from incidentpilot.persistence import repo


def test_settings_defaults(monkeypatch) -> None:
    monkeypatch.delenv("MAX_TOOL_CALLS", raising=False)
    reset_settings_cache()
    s = Settings()
    assert s.max_tool_calls > 0
    assert s.llm_enabled is False
    assert s.github_integration_enabled is False


@pytest.mark.asyncio
async def test_create_incident_and_run(db_session) -> None:
    incident = await repo.create_incident(
        db_session,
        title="Latency spike",
        service="orders-api",
        symptom="P95 latency up",
        severity="SEV2",
        repository="local/orders-api",
        environment="reference-production",
    )
    assert incident.incident_id.startswith("INC-")
    run = await repo.create_run(db_session, incident.id)
    assert run.run_id.startswith("RUN-")
    assert run.workflow_state == WorkflowState.INCIDENT_RECEIVED.value

    loaded = await repo.get_run_by_run_id(db_session, run.run_id)
    assert loaded is not None
    assert loaded.incident_pk == incident.id


@pytest.mark.asyncio
async def test_api_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["app"] == "IncidentPilot"


@pytest.mark.asyncio
async def test_create_incident_returns_202_and_run(client: AsyncClient) -> None:
    payload = {
        "title": "/orders latency spike",
        "service": "orders-api",
        "severity": "SEV2",
        "symptom": "P95 latency 400ms -> 2.8s, error rate 0.3% -> 7%",
        "repository": "local/orders-api",
        "environment": "reference-production",
    }
    resp = await client.post("/v1/incidents", json=payload)
    assert resp.status_code == 202
    data = resp.json()
    assert data["incident_id"]
    assert data["run_id"]
    assert data["status"] in {s.value for s in AgentRunStatus}

    # Allow background stub workflow to finish
    for _ in range(50):
        run_resp = await client.get(f"/v1/runs/{data['run_id']}")
        assert run_resp.status_code == 200
        run = run_resp.json()
        if run["status"] not in {AgentRunStatus.PENDING.value, AgentRunStatus.RUNNING.value}:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("run did not finish")

    assert run["status"] in {
        AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value,
        AgentRunStatus.INSUFFICIENT_EVIDENCE.value,
        AgentRunStatus.FAILED.value,
        AgentRunStatus.WAITING_APPROVAL.value,
        AgentRunStatus.RESOLVED.value,
    }
    assert run["root_cause"] is not None
    assert run["root_cause"].get("evidence_ids")

    evid = await client.get(f"/v1/runs/{data['run_id']}/evidence")
    assert evid.status_code == 200
    evidence = evid.json()
    assert len(evidence) >= 1

    tools = await client.get(f"/v1/runs/{data['run_id']}/tool-calls")
    assert tools.status_code == 200
    assert len(tools.json()) >= 1


@pytest.mark.asyncio
async def test_incident_not_found(client: AsyncClient) -> None:
    resp = await client.get("/v1/incidents/does-not-exist")
    assert resp.status_code == 404
