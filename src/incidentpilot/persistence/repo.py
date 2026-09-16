"""Repository helpers for control-plane entities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.models.db import AgentRun, Approval, AuditEvent, Evidence, Incident, ToolCall
from incidentpilot.models.enums import (
    AgentRunStatus,
    ApprovalDecision,
    IncidentStatus,
    WorkflowState,
)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


async def create_incident(
    session: AsyncSession,
    *,
    title: str,
    service: str,
    symptom: str,
    severity: str,
    repository: str,
    environment: str,
    start_time: datetime | None = None,
    incident_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Incident:
    incident = Incident(
        incident_id=incident_id or new_id("INC"),
        title=title,
        service=service,
        severity=severity,
        symptom=symptom,
        start_time=start_time or utcnow(),
        repository=repository,
        environment=environment,
        status=IncidentStatus.RECEIVED.value,
        payload=payload or {},
    )
    session.add(incident)
    await session.commit()
    await session.refresh(incident)
    return incident


async def create_run(
    session: AsyncSession,
    incident_pk: str,
    *,
    run_id: str | None = None,
    trace_id: str = "",
) -> AgentRun:
    run = AgentRun(
        run_id=run_id or new_id("RUN"),
        incident_pk=incident_pk,
        status=AgentRunStatus.PENDING.value,
        workflow_state=WorkflowState.INCIDENT_RECEIVED.value,
        trace_id=trace_id or uuid.uuid4().hex,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def get_incident_by_incident_id(session: AsyncSession, incident_id: str) -> Incident | None:
    result = await session.execute(
        select(Incident).where(Incident.incident_id == incident_id)
    )
    return result.scalar_one_or_none()


async def get_run_by_run_id(session: AsyncSession, run_id: str) -> AgentRun | None:
    result = await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))
    return result.scalar_one_or_none()


async def list_evidence(session: AsyncSession, run_pk: str) -> list[Evidence]:
    result = await session.execute(
        select(Evidence).where(Evidence.run_pk == run_pk).order_by(Evidence.created_at)
    )
    return list(result.scalars())


async def list_tool_calls(session: AsyncSession, run_pk: str) -> list[ToolCall]:
    result = await session.execute(
        select(ToolCall).where(ToolCall.run_pk == run_pk).order_by(ToolCall.created_at)
    )
    return list(result.scalars())


async def record_approval(
    session: AsyncSession,
    *,
    run_pk: str,
    decision: ApprovalDecision,
    actor: str = "human",
    reason: str = "",
    patch_artifact_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Approval:
    approval = Approval(
        run_pk=run_pk,
        decision=decision.value,
        actor=actor,
        reason=reason,
        patch_artifact_id=patch_artifact_id,
        payload=payload or {},
        decided_at=utcnow(),
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    return approval


async def record_audit(
    session: AsyncSession,
    *,
    event_type: str,
    message: str = "",
    run_pk: str | None = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        run_pk=run_pk,
        event_type=event_type,
        message=message,
        data=data or {},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event
