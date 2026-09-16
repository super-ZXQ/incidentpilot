"""Application service layer for control-plane operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.models.db import AgentRun, Incident
from incidentpilot.models.enums import AgentRunStatus, ApprovalDecision, IncidentStatus
from incidentpilot.persistence import repo


class IncidentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_incident(
        self,
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
        return await repo.create_incident(
            self.session,
            title=title,
            service=service,
            symptom=symptom,
            severity=severity,
            repository=repository,
            environment=environment,
            start_time=start_time,
            incident_id=incident_id,
            payload=payload,
        )

    async def get(self, incident_id: str) -> Incident | None:
        return await repo.get_incident_by_incident_id(self.session, incident_id)


class RunService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_for_incident(self, incident: Incident, trace_id: str = "") -> AgentRun:
        return await repo.create_run(self.session, incident.id, trace_id=trace_id)

    async def get(self, run_id: str) -> AgentRun | None:
        return await repo.get_run_by_run_id(self.session, run_id)

    async def mark_running(self, run: AgentRun) -> AgentRun:
        run.status = AgentRunStatus.RUNNING.value
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def mark_status(
        self,
        run: AgentRun,
        status: AgentRunStatus,
        *,
        workflow_state: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        root_cause: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> AgentRun:
        run.status = status.value
        if workflow_state is not None:
            run.workflow_state = workflow_state
        if error is not None:
            run.error = error
        if result is not None:
            run.result = result
        if root_cause is not None:
            run.root_cause = root_cause
        if finished:
            run.finished_at = repo.utcnow()
        await self.session.commit()
        await self.session.refresh(run)
        return run


class ApprovalService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def decide(
        self,
        run: AgentRun,
        decision: ApprovalDecision,
        *,
        actor: str = "human",
        reason: str = "",
    ):
        approval = await repo.record_approval(
            self.session,
            run_pk=run.id,
            decision=decision,
            actor=actor,
            reason=reason,
        )
        if decision == ApprovalDecision.REJECT:
            run.status = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
        else:
            run.status = AgentRunStatus.RUNNING.value
        await self.session.commit()
        await self.session.refresh(run)
        await self.session.refresh(approval)
        return approval, run

    async def update_incident_for_decision(self, incident: Incident, decision: ApprovalDecision) -> None:
        if decision == ApprovalDecision.REJECT:
            incident.status = IncidentStatus.NEEDS_HUMAN_INTERVENTION.value
        await self.session.commit()
