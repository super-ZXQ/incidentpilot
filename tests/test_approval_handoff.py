"""Approval handoff: the decision is durable, the resume belongs to a worker."""

from __future__ import annotations

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

from incidentpilot.config import reset_settings_cache
from incidentpilot.persistence.jobs import claim_next_run, create_incident_and_run
from incidentpilot.persistence.session import (
    dispose_engine,
    get_session_factory,
    init_db,
    reset_db_state,
)


async def _wait_for_status(client, run_id: str, wanted: set[str], tries: int = 150) -> dict:
    run: dict = {}
    for _ in range(tries):
        run = (await client.get(f"/v1/runs/{run_id}")).json()
        if run["status"] in wanted:
            return run
        await asyncio.sleep(0.1)
    pytest.fail(f"run stuck in {run.get('status')}: {run.get('error')}")


async def _claim(worker_id: str, run_id: str | None = None):
    async with get_session_factory()() as session:
        return await claim_next_run(
            session, worker_id=worker_id, lease_seconds=60, max_attempts=3, run_id=run_id
        )


@pytest.mark.asyncio
async def test_approval_enqueue_does_not_resume_inside_the_request(tmp_path) -> None:
    """A durable deployment records the decision and hands the resume to a worker."""
    reset_db_state()
    reset_settings_cache()
    os.environ["API_TOKENS"] = '{"handoff-token":{"subject":"handoff-admin","role":"admin"}}'
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'handoff.db'}"
    os.environ["TOOL_BACKEND"] = "fake"
    os.environ["EMBEDDED_WORKER_ENABLED"] = "false"
    os.environ["GITHUB_INTEGRATION_ENABLED"] = "false"
    os.environ["OTEL_CONSOLE_EXPORTER"] = "false"
    reset_settings_cache()
    await init_db()

    try:
        from incidentpilot.api.app import create_app
        from incidentpilot.worker import DurableWorker

        app = create_app()
        worker = DurableWorker(app.state.settings, worker_id="worker-handoff")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer handoff-token"},
        ) as client:
            resp = await client.post(
                "/v1/incidents",
                json={
                    "title": "orders latency handoff",
                    "service": "orders-api",
                    "severity": "SEV2",
                    "symptom": "P95 400ms -> 2.8s",
                    "repository": "local/orders-api",
                    "environment": "reference-production",
                },
            )
            run_id = resp.json()["run_id"]

            claimed = await _claim(worker.worker_id)
            assert claimed is not None and claimed.run_id == run_id
            await worker._execute_claim(claimed)
            await _wait_for_status(client, run_id, {"WAITING_APPROVAL"}, tries=5)

            approval = await client.post(
                f"/v1/runs/{run_id}/approval",
                json={"decision": "APPROVE", "actor": "handoff-admin", "reason": "validated"},
            )
            assert approval.status_code == 200

            # The request made the decision durable and did nothing else: no graph
            # resume, no GitHub call, so cancelling it cannot strand the run.
            queued = (await client.get(f"/v1/runs/{run_id}")).json()
            assert queued["status"] == "PENDING"
            assert queued["result"].get("pull_request") is None

            resumed = await _claim(worker.worker_id)
            assert resumed is not None and resumed.run_id == run_id
            await worker._execute_claim(resumed)

            resolved = await _wait_for_status(client, run_id, {"RESOLVED"}, tries=5)
            assert resolved["result"]["pull_request"]["mode"] == "mock"
    finally:
        os.environ.pop("EMBEDDED_WORKER_ENABLED", None)
        os.environ.pop("GITHUB_INTEGRATION_ENABLED", None)
        await dispose_engine()
        reset_db_state()
        reset_settings_cache()


@pytest.mark.asyncio
async def test_resume_honours_run_timeout(db_session, monkeypatch) -> None:
    """A hung resume is halted by the run budget instead of pinning a worker slot."""
    from incidentpilot.config import Settings
    from incidentpilot.models.db import Approval
    from incidentpilot.models.enums import AgentRunStatus, ApprovalDecision
    from incidentpilot.persistence.jobs import utcnow
    from incidentpilot.services.incidents import RunService
    from incidentpilot.worker import DurableWorker

    _, run = await create_incident_and_run(
        db_session,
        incident_data={"title": "resume timeout", "service": "orders-api", "symptom": "latency"},
        max_queued_runs=10,
    )
    db_session.add(
        Approval(
            run_pk=run.id,
            decision=ApprovalDecision.APPROVE.value,
            actor="tester",
            decided_at=utcnow(),
        )
    )
    await db_session.commit()
    claim = await claim_next_run(db_session, worker_id="worker-a", lease_seconds=60, max_attempts=3)
    assert claim is not None

    async def hanging_resume(**kwargs):
        await asyncio.sleep(30)
        return {"status": AgentRunStatus.RESOLVED.value}

    monkeypatch.setattr("incidentpilot.agent.graph.resume_agent_workflow", hanging_resume)
    worker = DurableWorker(Settings(run_timeout_seconds=1, tool_backend="fake"), worker_id=claim.worker_id)
    await worker._execute_claim(claim)

    current = await RunService(db_session).get(run.run_id)
    await db_session.refresh(current)
    assert current.status == AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
    assert current.error == "run timeout budget exhausted"


@pytest.mark.asyncio
async def test_heartbeat_survives_transient_renewal_failure(db_session, monkeypatch) -> None:
    """One failed renewal is a database blip, not a reason to abort a healthy run."""
    from incidentpilot.config import Settings
    from incidentpilot.worker import DurableWorker

    settings = Settings(job_heartbeat_seconds=1, job_lease_seconds=10, tool_backend="fake")
    worker = DurableWorker(settings, worker_id="worker-a")
    outcome = {"calls": 0, "owned": True}

    async def flaky_renew(session, **kwargs):
        outcome["calls"] += 1
        if outcome["calls"] == 1:
            raise TimeoutError("connection pool timeout")
        return outcome["owned"]

    monkeypatch.setattr("incidentpilot.worker.renew_lease", flaky_renew)
    task = asyncio.create_task(worker._heartbeat("RUN-heartbeat"))
    await asyncio.sleep(2.3)
    assert outcome["calls"] >= 2
    assert not task.done(), "a transient renewal error killed the heartbeat"

    outcome["owned"] = False
    await asyncio.wait_for(task, timeout=3)
    assert task.exception() is None
