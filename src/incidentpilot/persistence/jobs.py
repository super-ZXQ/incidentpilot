"""PostgreSQL-backed run admission, leasing, and recovery primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.models.db import AgentRun, Incident
from incidentpilot.models.enums import AgentRunStatus, Severity, WorkflowState
from incidentpilot.persistence import repo


class QueueFullError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedRun:
    incident_pk: str
    run_id: str
    worker_id: str
    attempt_count: int
    recover: bool


def utcnow() -> datetime:
    return datetime.now(UTC)


async def create_incident_and_run(
    session: AsyncSession,
    *,
    incident_data: dict[str, Any],
    max_queued_runs: int,
) -> tuple[Incident, AgentRun]:
    """Serialize admission and persist Incident + PENDING run in one transaction."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(73492101)"))
    queued = await session.scalar(
        select(func.count()).select_from(AgentRun).where(
            AgentRun.status == AgentRunStatus.PENDING.value
        )
    )
    if int(queued or 0) >= max_queued_runs:
        await session.rollback()
        raise QueueFullError("incident run queue is full")

    incident = Incident(
        incident_id=incident_data.get("incident_id") or repo.new_id("INC"),
        title=incident_data["title"],
        service=incident_data["service"],
        severity=incident_data.get("severity", Severity.SEV3.value),
        symptom=incident_data["symptom"],
        start_time=incident_data.get("start_time") or utcnow(),
        repository=incident_data.get("repository", ""),
        environment=incident_data.get("environment", "reference-production"),
        payload=incident_data.get("payload") or {},
    )
    run = AgentRun(
        run_id=repo.new_id("RUN"),
        incident=incident,
        status=AgentRunStatus.PENDING.value,
        workflow_state=WorkflowState.INCIDENT_RECEIVED.value,
        trace_id=repo.new_id("TRACE"),
    )
    session.add_all([incident, run])
    await session.commit()
    await session.refresh(incident)
    await session.refresh(run)
    return incident, run


async def claim_next_run(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
    run_id: str | None = None,
) -> ClaimedRun | None:
    """Atomically claim one pending or lease-expired run."""
    current = now or utcnow()
    eligible = or_(
        (
            (AgentRun.status == AgentRunStatus.PENDING.value)
            & or_(AgentRun.next_attempt_at.is_(None), AgentRun.next_attempt_at <= current)
        ),
        (
            (AgentRun.status == AgentRunStatus.RUNNING.value)
            & (AgentRun.lease_expires_at.is_not(None))
            & (AgentRun.lease_expires_at <= current)
        ),
    )
    predicates = [eligible, AgentRun.attempt_count < max_attempts]
    if run_id is not None:
        predicates.append(AgentRun.run_id == run_id)
    query = (
        select(AgentRun)
        .where(*predicates)
        .order_by(AgentRun.next_attempt_at.asc().nullsfirst(), AgentRun.started_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    run = (await session.execute(query)).scalar_one_or_none()
    if run is None:
        await session.rollback()
        return None
    recovering = run.status == AgentRunStatus.RUNNING.value
    run.status = AgentRunStatus.RUNNING.value
    run.worker_id = worker_id
    run.attempt_count += 1
    run.last_heartbeat_at = current
    run.lease_expires_at = current + timedelta(seconds=lease_seconds)
    run.next_attempt_at = None
    await session.commit()
    return ClaimedRun(
        incident_pk=run.incident_pk,
        run_id=run.run_id,
        worker_id=worker_id,
        attempt_count=run.attempt_count,
        recover=recovering,
    )


async def renew_lease(
    session: AsyncSession,
    *,
    run_id: str,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    current = now or utcnow()
    result = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.run_id == run_id,
            AgentRun.status == AgentRunStatus.RUNNING.value,
            AgentRun.worker_id == worker_id,
        )
        .values(
            last_heartbeat_at=current,
            lease_expires_at=current + timedelta(seconds=lease_seconds),
        )
    )
    await session.commit()
    return bool(result.rowcount)


async def release_claim(
    session: AsyncSession,
    *,
    run_id: str,
    worker_id: str,
) -> bool:
    result = await session.execute(
        update(AgentRun)
        .where(AgentRun.run_id == run_id, AgentRun.worker_id == worker_id)
        .values(worker_id=None, lease_expires_at=None, last_heartbeat_at=None)
    )
    await session.commit()
    return bool(result.rowcount)


async def retry_claim(
    session: AsyncSession,
    *,
    run_id: str,
    worker_id: str,
    max_attempts: int,
    failure_category: str,
    delay_seconds: float,
) -> str:
    run = (
        await session.execute(
            select(AgentRun)
            .where(AgentRun.run_id == run_id, AgentRun.worker_id == worker_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        await session.rollback()
        return "LOST_LEASE"
    run.worker_id = None
    run.lease_expires_at = None
    run.last_heartbeat_at = None
    run.failure_category = failure_category
    if run.attempt_count >= max_attempts:
        run.status = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
        run.workflow_state = WorkflowState.NEEDS_HUMAN_INTERVENTION.value
        run.error = f"job attempts exhausted: {failure_category}"
        run.finished_at = utcnow()
    else:
        run.status = AgentRunStatus.PENDING.value
        run.next_attempt_at = utcnow() + timedelta(seconds=delay_seconds)
        run.error = f"retry scheduled: {failure_category}"
    await session.commit()
    return run.status


async def expire_exhausted_runs(
    session: AsyncSession,
    *,
    max_attempts: int,
    now: datetime | None = None,
) -> int:
    current = now or utcnow()
    result = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.status == AgentRunStatus.RUNNING.value,
            AgentRun.lease_expires_at <= current,
            AgentRun.attempt_count >= max_attempts,
        )
        .values(
            status=AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value,
            workflow_state=WorkflowState.NEEDS_HUMAN_INTERVENTION.value,
            failure_category="lease_expired",
            error="job attempts exhausted after lease expiration",
            finished_at=current,
            worker_id=None,
            lease_expires_at=None,
            last_heartbeat_at=None,
        )
    )
    await session.commit()
    return int(result.rowcount or 0)
