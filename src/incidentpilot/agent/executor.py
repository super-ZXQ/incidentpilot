"""Async RunExecutor.

Source of truth is PostgreSQL + LangGraph checkpoint, not asyncio.Task.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from incidentpilot.config import Settings, get_settings
from incidentpilot.models.enums import RUN_TRANSITIONS, AgentRunStatus, WorkflowState
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

    def submit_resume(self, run_id: str, decision: str) -> None:
        """Continue an approved run without binding it to the caller's lifetime."""
        task = asyncio.create_task(self._resume_safe(run_id, decision), name=f"resume-{run_id}")
        self._tasks[run_id] = task

    async def approved_decision(self, run_id: str) -> str | None:
        """Return the persisted APPROVE decision that a resume must honour."""
        from sqlalchemy import select

        from incidentpilot.models.db import AgentRun, Approval
        from incidentpilot.models.enums import ApprovalDecision

        factory = get_session_factory()
        async with factory() as session:
            return await session.scalar(
                select(Approval.decision)
                .join(AgentRun, Approval.run_pk == AgentRun.id)
                .where(
                    AgentRun.run_id == run_id,
                    Approval.decision == ApprovalDecision.APPROVE.value,
                )
            )

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
            decision = await self.approved_decision(run.run_id)
            if decision is not None:
                self.submit_resume(run.run_id, decision)
            else:
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
            await self._halt(run_id, AgentRunStatus.FAILED, "executor crashed")
        finally:
            self._tasks.pop(run_id, None)

    async def _resume_safe(self, run_id: str, decision: str) -> None:
        try:
            await self.resume(run_id=run_id, decision=decision)
        except Exception:
            # A resume can fail after its external side effect already landed, so a
            # human has to reconcile it rather than the run being retried blindly.
            logger.exception("approval resume crashed for %s", run_id)
            await self._halt(
                run_id, AgentRunStatus.NEEDS_HUMAN_INTERVENTION, "approval resume crashed"
            )
        finally:
            self._tasks.pop(run_id, None)

    async def _halt(self, run_id: str, target: AgentRunStatus, error: str) -> None:
        factory = get_session_factory()
        async with factory() as session:
            service = RunService(session)
            run = await service.get(run_id)
            if run is None:
                return
            if target not in RUN_TRANSITIONS[AgentRunStatus(run.status)]:
                logger.warning("run %s left at %s after %s", run_id, run.status, error)
                return
            workflow_state = (
                WorkflowState.FAILED.value
                if target == AgentRunStatus.FAILED
                else WorkflowState.NEEDS_HUMAN_INTERVENTION.value
            )
            await service.mark_status(
                run,
                target,
                workflow_state=workflow_state,
                error=error[:2000],
                finished=True,
            )

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
                await self._halt(
                    run_id, AgentRunStatus.NEEDS_HUMAN_INTERVENTION, "run timeout budget exhausted"
                )
                return self._halted(run_id)
            except Exception as exc:
                from incidentpilot.llm.provider import StructuredOutputError

                if not isinstance(exc, StructuredOutputError):
                    raise
                await self._halt(run_id, AgentRunStatus.NEEDS_HUMAN_INTERVENTION, str(exc))
                return self._halted(run_id)

    async def resume(self, *, run_id: str, decision: str) -> dict[str, Any]:
        """Continue a run parked on the approval interrupt, inside the run timeout."""
        from incidentpilot.agent.graph import resume_agent_workflow
        from incidentpilot.observability.otel import configure_otel, start_span

        configure_otel(self.settings)
        factory = get_session_factory()
        async with factory() as session:
            service = RunService(session)
            run = await service.get(run_id)
            if run is None:
                raise ValueError(f"run not found: {run_id}")
            await service.mark_running(run)
        with start_span("agent_resume", {"run_id": run_id, "decision": decision}):
            try:
                return await asyncio.wait_for(
                    resume_agent_workflow(
                        run_id=run_id, decision=decision, settings=self.settings
                    ),
                    timeout=self.settings.run_timeout_seconds,
                )
            except TimeoutError:
                await self._halt(
                    run_id, AgentRunStatus.NEEDS_HUMAN_INTERVENTION, "run timeout budget exhausted"
                )
                return self._halted(run_id)

    @staticmethod
    def _halted(run_id: str) -> dict[str, Any]:
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
