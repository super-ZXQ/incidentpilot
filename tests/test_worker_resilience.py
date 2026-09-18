"""Durable queue, lease, backpressure, and retry behavior."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import select

from incidentpilot.agent.graph import _persist_patch_artifact_once
from incidentpilot.config import Settings
from incidentpilot.llm.provider import FakeLLMProvider, LLMMessage, StructuredOutputError
from incidentpilot.models.db import AgentRun
from incidentpilot.models.enums import AgentRunStatus
from incidentpilot.persistence.jobs import (
    claim_next_run,
    create_incident_and_run,
    retry_claim,
    utcnow,
)
from incidentpilot.persistence.session import dispose_engine, init_db, reset_db_state
from incidentpilot.sandbox.manager import PatchArtifactData
from incidentpilot.tools.gateway import BudgetTracker, ToolGateway, ToolRegistry, ToolSpec


def incident_data(title: str = "lease test") -> dict[str, str]:
    return {"title": title, "service": "orders-api", "symptom": "latency"}


@pytest.mark.asyncio
async def test_prometheus_metrics_expose_required_names(client) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    for name in (
        "incidentpilot_queue_depth",
        "incidentpilot_active_runs",
        "incidentpilot_run_duration_seconds",
        "incidentpilot_run_retries_total",
        "incidentpilot_run_recoveries_total",
        "incidentpilot_lease_expirations_total",
        "incidentpilot_tool_failures_total",
        "incidentpilot_duplicate_side_effect_prevented_total",
        "incidentpilot_runs_total",
    ):
        assert name in body


@pytest.mark.asyncio
async def test_lease_exclusion_expiration_and_attempt_limit(db_session) -> None:
    _, run = await create_incident_and_run(
        db_session, incident_data=incident_data(), max_queued_runs=10
    )
    now = utcnow()
    first = await claim_next_run(
        db_session, worker_id="worker-a", lease_seconds=30, max_attempts=2, now=now
    )
    assert first is not None
    assert first.run_id == run.run_id
    assert first.recover is False
    assert (
        await claim_next_run(
            db_session, worker_id="worker-b", lease_seconds=30, max_attempts=2, now=now
        )
        is None
    )

    recovered = await claim_next_run(
        db_session,
        worker_id="worker-b",
        lease_seconds=30,
        max_attempts=2,
        now=now + timedelta(seconds=31),
    )
    assert recovered is not None
    assert recovered.recover is True
    assert recovered.attempt_count == 2
    assert (
        await claim_next_run(
            db_session,
            worker_id="worker-c",
            lease_seconds=30,
            max_attempts=2,
            now=now + timedelta(seconds=62),
        )
        is None
    )


@pytest.mark.asyncio
async def test_max_attempts_transitions_to_human(db_session) -> None:
    _, run = await create_incident_and_run(
        db_session, incident_data=incident_data(), max_queued_runs=10
    )
    claim = await claim_next_run(
        db_session, worker_id="worker-a", lease_seconds=30, max_attempts=1
    )
    assert claim is not None
    status = await retry_claim(
        db_session,
        run_id=run.run_id,
        worker_id="worker-a",
        max_attempts=1,
        failure_category="database_connection",
        delay_seconds=0,
    )
    assert status == AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value


@pytest.mark.asyncio
async def test_queue_full_returns_stable_429(tmp_path, monkeypatch) -> None:
    reset_db_state()
    url = f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    from incidentpilot.api.app import create_app

    settings = Settings(
        database_url=url,
        embedded_worker_enabled=False,
        max_queued_runs=1,
        tool_backend="fake",
    )
    await init_db(url)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings)), base_url="http://test"
    ) as client:
        body = {"title": "one", "service": "orders-api", "symptom": "latency"}
        assert (await client.post("/v1/incidents", json=body)).status_code == 202
        response = await client.post("/v1/incidents", json={**body, "title": "two"})
        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "RUN_QUEUE_FULL"
    await dispose_engine()
    reset_db_state()


@pytest.mark.asyncio
async def test_transient_error_retries_with_backoff() -> None:
    calls = 0
    delays: list[float] = []

    async def flaky() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionResetError("reset")
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec("read", "read", "readonly_external", flaky, max_retries=2))
    gateway = ToolGateway(
        registry,
        BudgetTracker(3, 3),
        sleep=lambda delay: delays.append(delay),
        jitter=lambda: 0.5,
    )
    result = await gateway.call("read", {}, trace_id="trace-test")
    assert result.status == "SUCCEEDED"
    assert result.attempt_count == 3
    assert calls == 3
    assert delays == [0.25, 0.5]


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried() -> None:
    calls = 0

    async def unsafe() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        raise PermissionError("denied")

    registry = ToolRegistry()
    registry.register(ToolSpec("write", "write", "readonly_external", unsafe, max_retries=5))
    result = await ToolGateway(registry).call("write", {})
    assert result.status == "FAILED"
    assert result.error_category == "permission"
    assert result.attempt_count == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_terminal_run_cannot_be_reclaimed(db_session) -> None:
    _, created = await create_incident_and_run(
        db_session, incident_data=incident_data(), max_queued_runs=10
    )
    run = (await db_session.execute(select(AgentRun).where(AgentRun.id == created.id))).scalar_one()
    run.status = AgentRunStatus.RESOLVED.value
    run.lease_expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    assert (
        await claim_next_run(
            db_session, worker_id="worker", lease_seconds=30, max_attempts=3
        )
        is None
    )


class _StructuredAnswer(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_invalid_llm_structure_has_bounded_repair() -> None:
    provider = FakeLLMProvider(responses=["bad", "still bad", "also bad"])
    with pytest.raises(StructuredOutputError):
        await provider.complete_structured(
            _StructuredAnswer, [LLMMessage(role="user", content="answer")]
        )
    assert provider._calls == 3


@pytest.mark.asyncio
async def test_patch_artifact_checkpoint_replay_is_idempotent(db_session) -> None:
    _, run = await create_incident_and_run(
        db_session, incident_data=incident_data(), max_queued_runs=10
    )
    artifact = PatchArtifactData(
        patch_artifact_id="PATCH-first",
        base_commit_sha="abc",
        patch_diff="--- a/a.py\n+++ b/a.py\n",
        patch_hash="same-hash",
        test_run_id="TEST-one",
        created_at=utcnow().isoformat(),
    )
    first = await _persist_patch_artifact_once(run.run_id, artifact)
    artifact.patch_artifact_id = "PATCH-replayed"
    second = await _persist_patch_artifact_once(run.run_id, artifact)
    assert first == second == "PATCH-first"
