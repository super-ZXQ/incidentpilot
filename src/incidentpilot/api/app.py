"""FastAPI application factory and routes."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.agent.executor import get_run_executor
from incidentpilot.api.auth import Principal, authenticate
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
    TestRunOut,
    ToolCallOut,
)
from incidentpilot.observability import metrics as worker_metrics
from incidentpilot.persistence import repo
from incidentpilot.persistence.jobs import QueueFullError, create_incident_and_run
from incidentpilot.persistence.session import get_session, init_db
from incidentpilot.services.incidents import (
    ApprovalConflict,
    ApprovalService,
    IncidentService,
    RunService,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    await init_db()
    if settings.embedded_worker_enabled:
        await get_run_executor().recover_non_terminal()
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.embedded_worker_enabled and settings.environment not in {"development", "test"}:
        raise ValueError("protected environment requires separate durable workers")
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.settings = settings
    demo = Path(__file__).with_name("static")
    app.mount("/demo-assets", StaticFiles(directory=demo), name="demo-assets")

    @app.get("/demo", include_in_schema=False)
    async def investigation_demo():
        return FileResponse(demo / "demo.html", headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })

    @app.get("/v1/runtime")
    async def runtime(principal: Principal = Depends(authenticate)):
        return {"model": "real" if settings.llm_enabled and settings.llm_api_key else "fake",
                "github": "real-enabled" if settings.github_integration_enabled else "mock",
                "sandbox": "docker" if settings.sandbox_enabled else "local",
                "role": principal.role}

    async def session_dep() -> AsyncIterator[AsyncSession]:
        async for session in get_session():
            yield session

    @app.get("/health", response_model=HealthOut)
    async def health() -> HealthOut:
        return HealthOut(app=settings.app_name, environment=settings.environment)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(principal: Principal = Depends(authenticate)) -> Response:
        principal.require("reviewer", "approver")
        return Response(
            generate_latest(worker_metrics.PROMETHEUS_REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.post(
        "/v1/incidents",
        response_model=CreateRunAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_incident(
        body: IncidentCreate,
        session: AsyncSession = Depends(session_dep),
        principal: Principal = Depends(authenticate),
    ) -> CreateRunAccepted:
        principal.require("initiator")
        incident_service = IncidentService(session)

        if body.incident_id:
            existing = await incident_service.get(body.incident_id)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"incident_id already exists: {body.incident_id}",
                )

        try:
            incident, run = await create_incident_and_run(
                session,
                incident_data={
                    "owner_id": principal.subject,
                    "title": body.title,
                    "service": body.service,
                    "symptom": body.symptom,
                    "severity": body.severity.value,
                    "repository": body.repository,
                    "environment": body.environment,
                    "start_time": body.start_time,
                    "incident_id": body.incident_id,
                    "payload": body.payload,
                },
                max_queued_runs=settings.max_queued_runs,
            )
        except QueueFullError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "RUN_QUEUE_FULL", "message": str(exc)},
            ) from exc

        # Fire-and-forget background execution; durable state lives in DB.
        if settings.embedded_worker_enabled:
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
        principal: Principal = Depends(authenticate),
    ) -> IncidentOut:
        incident = await IncidentService(session).get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        principal.access(incident.owner_id)
        return IncidentOut.model_validate(incident)

    async def authorize_run(session: AsyncSession, run, principal: Principal):
        from incidentpilot.models.db import Incident

        incident = await session.get(Incident, run.incident_pk)
        principal.access(incident.owner_id if incident else None)
        return incident

    @app.get("/v1/runs/{run_id}", response_model=RunOut)
    async def get_run(run_id: str, session: AsyncSession = Depends(session_dep), principal: Principal = Depends(authenticate)) -> RunOut:
        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        incident = await authorize_run(session, run, principal)
        return RunOut.model_validate(run).model_copy(update={"incident_id": incident.incident_id})

    @app.get("/v1/runs/{run_id}/evidence", response_model=list[EvidenceOut])
    async def list_evidence(
        run_id: str, session: AsyncSession = Depends(session_dep), principal: Principal = Depends(authenticate)
    ) -> list[EvidenceOut]:
        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        await authorize_run(session, run, principal)
        items = await repo.list_evidence(session, run.id)
        return [EvidenceOut.model_validate(x) for x in items]

    @app.get("/v1/runs/{run_id}/tool-calls", response_model=list[ToolCallOut])
    async def list_tool_calls(
        run_id: str, session: AsyncSession = Depends(session_dep), principal: Principal = Depends(authenticate)
    ) -> list[ToolCallOut]:
        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        await authorize_run(session, run, principal)
        items = await repo.list_tool_calls(session, run.id)
        return [ToolCallOut.model_validate(x) for x in items]

    @app.get("/v1/runs/{run_id}/patch-artifact", response_model=list[PatchArtifactOut])
    async def list_patch_artifacts(
        run_id: str, session: AsyncSession = Depends(session_dep), principal: Principal = Depends(authenticate)
    ) -> list[PatchArtifactOut]:
        from sqlalchemy import select

        from incidentpilot.models.db import PatchArtifact

        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        await authorize_run(session, run, principal)
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
        principal: Principal = Depends(authenticate),
    ) -> ApprovalOut:
        principal.require("approver")
        run_service = RunService(session)
        run = await run_service.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        await authorize_run(session, run, principal)
        if run.status != AgentRunStatus.WAITING_APPROVAL.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run is not waiting for approval (status={run.status})",
            )

        approval_service = ApprovalService(session)
        try:
            approval, run = await approval_service.decide(
                run,
                body.decision,
                actor=principal.subject,
                patch_artifact_id=body.patch_artifact_id,
                patch_hash=body.patch_hash,
                reason=body.reason,
                lease_seconds=settings.job_lease_seconds,
                owner_id=f"approval-api-{uuid.uuid4().hex[:8]}",
            )
        except ApprovalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        from incidentpilot.models.db import Incident

        incident = await session.get(Incident, run.incident_pk)
        if incident is not None:
            await approval_service.update_incident_for_decision(incident, body.decision)

        # Resume the persisted LangGraph interrupt. GitHub is invoked by the
        # resumed graph node, never directly by this API route.
        from incidentpilot.agent.graph import resume_agent_workflow
        if run.worker_id:
            from incidentpilot.persistence.jobs import ClaimedRun
            from incidentpilot.worker import DurableWorker

            await DurableWorker(settings, worker_id=run.worker_id)._execute_claim(
                ClaimedRun(run.incident_pk, run.run_id, run.worker_id, run.attempt_count, True)
            )
        else:
            await resume_agent_workflow(run_id=run.run_id, decision=body.decision.value, settings=settings)


        return ApprovalOut.model_validate(approval)

    @app.get("/v1/runs/{run_id}/tests", response_model=list[TestRunOut])
    async def list_tests(run_id: str, session: AsyncSession = Depends(session_dep), principal: Principal = Depends(authenticate)):
        from sqlalchemy import select

        from incidentpilot.models.db import TestRun

        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        await authorize_run(session, run, principal)
        return list((await session.execute(select(TestRun).where(TestRun.run_pk == run.id))).scalars())

    @app.get("/v1/runs/{run_id}/approval", response_model=ApprovalOut | None)
    async def get_approval(run_id: str, session: AsyncSession = Depends(session_dep), principal: Principal = Depends(authenticate)):
        from sqlalchemy import select

        from incidentpilot.models.db import Approval

        run = await RunService(session).get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        await authorize_run(session, run, principal)
        return await session.scalar(select(Approval).where(Approval.run_pk == run.id))

    return app


app = create_app()
