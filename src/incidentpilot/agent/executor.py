"""Async RunExecutor.

Source of truth is PostgreSQL + LangGraph checkpoint, not asyncio.Task.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from incidentpilot.config import Settings, get_settings
from incidentpilot.models.enums import AgentRunStatus, WorkflowState
from incidentpilot.persistence.session import get_session_factory
from incidentpilot.services.incidents import RunService

logger = logging.getLogger(__name__)


class RunExecutor:
    """Single-process async executor for Agent runs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def submit(self, incident_pk: str, run_id: str, *, recover: bool = False) -> None:
        task = asyncio.create_task(
            self._run_safe(incident_pk, run_id, recover=recover), name=f"run-{run_id}"
        )
        self._tasks[run_id] = task

    async def recover_non_terminal(self) -> int:
        """Reschedule DB-backed non-terminal runs after process startup."""
        from sqlalchemy import select

        from incidentpilot.models.db import AgentRun

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(AgentRun).where(
                    AgentRun.status.in_(
                        [AgentRunStatus.PENDING.value, AgentRunStatus.RUNNING.value]
                    )
                )
            )
            runs = list(result.scalars())
        for run in runs:
            self.submit(
                run.incident_pk,
                run.run_id,
                recover=run.status == AgentRunStatus.RUNNING.value,
            )
        return len(runs)

    async def wait_for(self, run_id: str, timeout: float | None = None) -> None:
        task = self._tasks.get(run_id)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            logger.warning("run %s wait timed out", run_id)

    async def _run_safe(self, incident_pk: str, run_id: str, *, recover: bool = False) -> None:
        try:
            await self.execute(incident_pk=incident_pk, run_id=run_id, recover=recover)
        except Exception:
            logger.exception("run %s crashed", run_id)
            factory = get_session_factory()
            async with factory() as session:
                service = RunService(session)
                run = await service.get(run_id)
                if run is not None:
                    await service.mark_status(
                        run,
                        AgentRunStatus.FAILED,
                        workflow_state=WorkflowState.FAILED.value,
                        error="executor crashed",
                        finished=True,
                    )
        finally:
            self._tasks.pop(run_id, None)

    async def execute(
        self, *, incident_pk: str, run_id: str, recover: bool = False
    ) -> dict[str, Any]:
        """Execute one agent run."""
        from incidentpilot.agent.graph import run_agent_workflow
        from incidentpilot.observability.otel import configure_otel, start_span

        configure_otel(self.settings)
        with start_span(
            "agent_run",
            {"run_id": run_id, "incident_pk": incident_pk},
        ):
            try:
                return await asyncio.wait_for(
                    run_agent_workflow(
                        incident_pk=incident_pk,
                        run_id=run_id,
                        settings=self.settings,
                        recover=recover,
                    ),
                    timeout=self.settings.run_timeout_seconds,
                )
            except TimeoutError:
                factory = get_session_factory()
                async with factory() as session:
                    run = await RunService(session).get(run_id)
                    if run is not None:
                        await RunService(session).mark_status(
                            run,
                            AgentRunStatus.NEEDS_HUMAN_INTERVENTION,
                            workflow_state=WorkflowState.NEEDS_HUMAN_INTERVENTION.value,
                            error="run timeout budget exhausted",
                            finished=True,
                        )
                return {"run_id": run_id, "status": AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value}


_executor: RunExecutor | None = None


def get_run_executor() -> RunExecutor:
    global _executor
    if _executor is None:
        _executor = RunExecutor()
    return _executor


def reset_run_executor() -> None:
    global _executor
    _executor = None


__all__ = ["RunExecutor", "get_run_executor", "reset_run_executor"]
