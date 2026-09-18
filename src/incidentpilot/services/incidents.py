"""Application service layer for control-plane operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.models.db import AgentRun, Approval, Incident
from incidentpilot.models.enums import (
    TERMINAL_RUN_STATUSES,
    AgentRunStatus,
    ApprovalDecision,
    IncidentStatus,
    WorkflowState,
)
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
        changed = await self.session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run.id,
                AgentRun.status.in_(
                    [AgentRunStatus.PENDING.value, AgentRunStatus.RUNNING.value]
                ),
            )
            .values(status=AgentRunStatus.RUNNING.value)
        )
        if not changed.rowcount:
            await self.session.rollback()
            raise RuntimeError(f"terminal run cannot return to RUNNING: {run.run_id}")
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
        terminal_values = {item.value for item in TERMINAL_RUN_STATUSES}
        if run.status in terminal_values and run.status != status.value:
            raise RuntimeError(
                f"terminal run transition denied: {run.status} -> {status.value}"
            )
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
            run.worker_id = None
            run.lease_expires_at = None
            run.last_heartbeat_at = None
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
        lease_seconds: int = 60,
        owner_id: str = "approval-api",
    ):
        target = (
            AgentRunStatus.NEEDS_HUMAN_INTERVENTION
            if decision == ApprovalDecision.REJECT
            else AgentRunStatus.RUNNING
        )
        workflow_state = (
            WorkflowState.NEEDS_HUMAN_INTERVENTION.value
            if decision == ApprovalDecision.REJECT
            else WorkflowState.CREATE_PULL_REQUEST.value
        )
        claimed = await self.session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run.id,
                AgentRun.status == AgentRunStatus.WAITING_APPROVAL.value,
            )
            .values(
                status=target.value,
                workflow_state=workflow_state,
                worker_id=owner_id if decision == ApprovalDecision.APPROVE else None,
                last_heartbeat_at=(repo.utcnow() if decision == ApprovalDecision.APPROVE else None),
                lease_expires_at=(
                    repo.utcnow() + timedelta(seconds=lease_seconds)
                    if decision == ApprovalDecision.APPROVE
                    else None
                ),
            )
        )
        if not claimed.rowcount:
            await self.session.rollback()
            raise ApprovalConflict("approval was already decided")
        approval = Approval(
            run_pk=run.id,
            decision=decision.value,
            actor=actor,
            reason=reason,
            decided_at=repo.utcnow(),
        )
        self.session.add(approval)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ApprovalConflict("approval was already decided") from exc
        await self.session.refresh(run)
        await self.session.refresh(approval)
        return approval, run

    async def update_incident_for_decision(self, incident: Incident, decision: ApprovalDecision) -> None:
        if decision == ApprovalDecision.REJECT:
            incident.status = IncidentStatus.NEEDS_HUMAN_INTERVENTION.value
        await self.session.commit()


class ApprovalConflict(RuntimeError):
    """Another request already committed the decision for this run."""
