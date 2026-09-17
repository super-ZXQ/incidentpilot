"""FastAPI application factory and routes."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.agent.executor import get_run_executor
from incidentpilot.config import Settings, get_settings
from incidentpilot.models.enums import AgentRunStatus
from incidentpilot.models.schemas import (
    ApprovalOut,
    ApprovalRequest,
    CreateRunAccepted,
    EvidenceOut,
    HealthOut,
    IncidentCreate,
    IncidentOut,
    PatchArtifactOut,
    RunOut,
    ToolCallOut,
)
from incidentpilot.persistence import repo
from incidentpilot.persistence.session import get_session, init_db
from incidentpilot.services.incidents import ApprovalService, IncidentService, RunService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    await init_db()
    if settings.database_url.startswith("postgresql"):
        await get_run_executor().recover_non_terminal()
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    async def session_dep() -> AsyncIterator[AsyncSession]:
        async for session in get_session():
            yield session

    @app.get("/health", response_model=HealthOut)
    async def health() -> HealthOut:
        return HealthOut(app=settings.app_name, environment=settings.environment)

    @app.post(
        "/v1/incidents",
        response_model=CreateRunAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_incident(
        body: IncidentCreate,
        session: AsyncSession = Depends(session_dep),
    ) -> CreateRunAccepted:
        incident_service = IncidentService(session)
        run_service = RunService(session)

        if body.incident_id:
            existing = await incident_service.get(body.incident_id)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"incident_id already exists: {body.incident_id}",
                )

        incident = await incident_service.create_incident(
            title=body.title,
            service=body.service,
            symptom=body.symptom,
            severity=body.severity.value,
            repository=body.repository,
            environment=body.environment,
            start_time=body.start_time,
            incident_id=body.incident_id,
            payload=body.payload,
        )
        run = await run_service.create_for_incident(incident)

        # Fire-and-forget background execution; durable state lives in DB.
        get_run_executor().submit(incident_pk=incident.id, run_id=run.run_id)

        return CreateRunAccepted(
            incident_id=incident.incident_id,
            run_id=run.run_id,
            status=AgentRunStatus(run.status),
            workflow_state=run.workflow_state,
        )

    @app.get("/v1/incidents/{incident_id}", response_model=IncidentOut)
    async def get_incident(
        incident_id: str,
        session: AsyncSession = Depends(session_dep),
    ) -> IncidentOut:
        incident = await IncidentService(session).get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return IncidentOut.model_validate(incident)

    @app.get("/v1/runs/{run_id}", response_model=RunOut)
    async def get_run(run_id: str, session: AsyncSession = Depends(session_dep)) -> RunOut:
        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return RunOut.model_validate(run)

    @app.get("/v1/runs/{run_id}/evidence", response_model=list[EvidenceOut])
    async def list_evidence(
        run_id: str, session: AsyncSession = Depends(session_dep)
    ) -> list[EvidenceOut]:
        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        items = await repo.list_evidence(session, run.id)
        return [EvidenceOut.model_validate(x) for x in items]

    @app.get("/v1/runs/{run_id}/tool-calls", response_model=list[ToolCallOut])
    async def list_tool_calls(
        run_id: str, session: AsyncSession = Depends(session_dep)
    ) -> list[ToolCallOut]:
        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        items = await repo.list_tool_calls(session, run.id)
        return [ToolCallOut.model_validate(x) for x in items]

    @app.get("/v1/runs/{run_id}/patch-artifact", response_model=list[PatchArtifactOut])
    async def list_patch_artifacts(
        run_id: str, session: AsyncSession = Depends(session_dep)
    ) -> list[PatchArtifactOut]:
        from sqlalchemy import select

        from incidentpilot.models.db import PatchArtifact

        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        result = await session.execute(
            select(PatchArtifact).where(PatchArtifact.run_pk == run.id)
        )
        items = list(result.scalars())
        return [PatchArtifactOut.model_validate(x) for x in items]

    @app.post("/v1/runs/{run_id}/approval", response_model=ApprovalOut)
    async def submit_approval(
        run_id: str,
        body: ApprovalRequest,
        session: AsyncSession = Depends(session_dep),
    ) -> ApprovalOut:
        run_service = RunService(session)
        run = await run_service.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.status != AgentRunStatus.WAITING_APPROVAL.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run is not waiting for approval (status={run.status})",
            )

        approval_service = ApprovalService(session)
        approval, run = await approval_service.decide(
            run,
            body.decision,
            actor=body.actor,
            reason=body.reason,
        )
        from incidentpilot.models.db import Incident

        incident = await session.get(Incident, run.incident_pk)
        if incident is not None:
            await approval_service.update_incident_for_decision(incident, body.decision)

        # Resume the persisted LangGraph interrupt. GitHub is invoked by the
        # resumed graph node, never directly by this API route.
        from incidentpilot.agent.graph import resume_agent_workflow

        await resume_agent_workflow(
            run_id=run.run_id,
            decision=body.decision.value,
            settings=settings,
        )

        return ApprovalOut.model_validate(approval)

    return app


app = create_app()
