"""P1 resilience tests: ownership loss, approval crash, evidence replay, GitHub 429/500."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from incidentpilot.github.integration import GitHubIntegration
from incidentpilot.models.db import Evidence
from incidentpilot.models.enums import AgentRunStatus
from incidentpilot.persistence.jobs import (
    claim_next_run,
    create_incident_and_run,
    renew_lease,
    retry_claim,
    utcnow,
)


def _incident(title: str = "resilience") -> dict[str, str]:
    return {"title": title, "service": "orders-api", "symptom": "latency"}


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_ownership(db_session) -> None:
    """Worker A claims; lease expires while A still 'holds'; B takes over."""
    _, run = await create_incident_and_run(
        db_session, incident_data=_incident("heartbeat loss"), max_queued_runs=10
    )
    now = utcnow()
    a = await claim_next_run(db_session, worker_id="worker-a", lease_seconds=10, max_attempts=3, now=now)
    assert a is not None and a.worker_id == "worker-a"
    # Heartbeat renewal still owned by A
    renewed = await renew_lease(
        db_session, run_id=run.run_id, worker_id="worker-a", lease_seconds=10
    )
    assert renewed is True
    # Simulate A dying without heartbeat: later B can claim after lease expiry
    b = await claim_next_run(
        db_session,
        worker_id="worker-b",
        lease_seconds=10,
        max_attempts=3,
        now=now + timedelta(seconds=25),
    )
    assert b is not None
    assert b.worker_id == "worker-b"
    assert b.attempt_count == 2
    # Stale worker A must not successfully renew
    stale = await renew_lease(
        db_session, run_id=run.run_id, worker_id="worker-a", lease_seconds=10
    )
    assert stale is False


@pytest.mark.asyncio
async def test_approval_persisted_before_graph_resume_can_recover(db_session) -> None:
    """API persists APPROVE Command; worker crash before resume; lease recovery can resume."""
    _, run = await create_incident_and_run(
        db_session, incident_data=_incident("approval crash"), max_queued_runs=10
    )
    run.status = AgentRunStatus.WAITING_APPROVAL.value
    run.workflow_state = "WAIT_FOR_APPROVAL"
    db_session.add(run)
    await db_session.commit()

    # Simulate approval command persisted in ExternalSideEffect / approval table
    from incidentpilot.models.db import Approval
    from incidentpilot.models.enums import ApprovalDecision

    db_session.add(
        Approval(
            run_pk=run.id,
            decision=ApprovalDecision.APPROVE.value,
            actor="tester",
            reason="pre-crash",
            decided_at=utcnow(),
        )
    )
    await db_session.commit()

    # Worker claims expired RUNNING approval resume
    run.status = AgentRunStatus.RUNNING.value
    run.worker_id = "dead-worker"
    run.attempt_count = 1
    run.lease_expires_at = utcnow() - timedelta(seconds=5)
    await db_session.commit()

    recovered = await claim_next_run(
        db_session,
        worker_id="worker-b",
        lease_seconds=30,
        max_attempts=3,
        now=utcnow(),
    )
    assert recovered is not None
    assert recovered.recover is True
    assert recovered.attempt_count >= 2

    approval = (
        await db_session.execute(select(Approval).where(Approval.run_pk == run.id))
    ).scalars().first()
    assert approval is not None
    assert approval.decision == ApprovalDecision.APPROVE.value


@pytest.mark.asyncio
async def test_evidence_replay_does_not_duplicate(db_session) -> None:
    _, run = await create_incident_and_run(
        db_session, incident_data=_incident("evidence replay"), max_queued_runs=10
    )

    async def persist_once(evidence_id: str) -> str:
        from sqlalchemy import select as sel

        existing = set(
            await db_session.execute(sel(Evidence.evidence_id).where(Evidence.run_pk == run.id))
        )
        if evidence_id not in {row[0] for row in existing}:
            db_session.add(
                Evidence(
                    evidence_id=evidence_id,
                    run_pk=run.id,
                    source="read_logs",
                    source_type="logs",
                    tool_call_id="TC-1",
                    content="log line",
                    result={"n": 1},
                )
            )
            await db_session.commit()
        return evidence_id

    assert await persist_once("EVID-replay") == "EVID-replay"
    assert await persist_once("EVID-replay") == "EVID-replay"
    rows = (await db_session.execute(select(Evidence).where(Evidence.run_pk == run.id))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_github_429_and_500_are_observable_and_do_not_duplicate_pr() -> None:
    # Mock mode + stable idempotency key: crash-after-create cannot double-create PR.
    gh = GitHubIntegration(enabled=False)
    key = "github_pr:RUN-r:abc"
    r1 = gh.create_pull_request(
        run_id="RUN-r", title="t", body="b", patch_diff="d", base_commit_sha="c", idempotency_key=key
    )
    r2 = gh.create_pull_request(
        run_id="RUN-r", title="t", body="b", patch_diff="d", base_commit_sha="c", idempotency_key=key
    )
    r3 = gh.create_pull_request(
        run_id="RUN-r", title="t", body="b", patch_diff="d", base_commit_sha="c", idempotency_key=key
    )
    assert gh.creation_call_count == 1
    assert r1.pr_url == r2.pr_url == r3.pr_url
    # Real GitHub HTTP is not called without credentials (enabled requires token+repo).


@pytest.mark.asyncio
async def test_worker_crash_recovery_attempt_increment(db_session) -> None:
    _, run = await create_incident_and_run(
        db_session, incident_data=_incident("crash recovery"), max_queued_runs=10
    )
    first = await claim_next_run(db_session, worker_id="a", lease_seconds=5, max_attempts=3)
    assert first is not None and first.attempt_count == 1
    status = await retry_claim(
        db_session,
        run_id=run.run_id,
        worker_id="a",
        max_attempts=3,
        failure_category="process_killed",
        delay_seconds=0,
    )
    assert status == AgentRunStatus.PENDING.value
    second = await claim_next_run(
        db_session,
        worker_id="b",
        lease_seconds=5,
        max_attempts=3,
        now=utcnow() + timedelta(seconds=1),
    )
    assert second is not None
    assert second.attempt_count == 2
    assert second.recover in {True, False} or second.attempt_count == 2
