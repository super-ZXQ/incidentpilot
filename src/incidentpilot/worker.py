"""PostgreSQL-leased worker entry point: ``python -m incidentpilot.worker``."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import time
import uuid
from contextlib import suppress

from sqlalchemy import func, select

from incidentpilot.agent.executor import RunExecutor
from incidentpilot.config import Settings, get_settings
from incidentpilot.models.db import AgentRun
from incidentpilot.models.enums import AgentRunStatus
from incidentpilot.observability.metrics import (
    ACTIVE_RUNS,
    LEASE_EXPIRATIONS,
    QUEUE_DEPTH,
    RUN_DURATION,
    RUN_RECOVERIES,
    RUN_RETRIES,
    RUNS,
)
from incidentpilot.persistence.jobs import (
    ClaimedRun,
    claim_next_run,
    expire_exhausted_runs,
    release_claim,
    renew_lease,
    retry_claim,
)
from incidentpilot.persistence.session import get_session_factory

logger = logging.getLogger(__name__)


class DurableWorker:
    """Bounded worker whose ownership is represented by a database lease."""

    def __init__(self, settings: Settings | None = None, *, worker_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = worker_id or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        self.executor = RunExecutor(self.settings)
        self._stopping = asyncio.Event()
        self._active: set[asyncio.Task[None]] = set()

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        factory = get_session_factory()
        while not self._stopping.is_set():
            self._active = {task for task in self._active if not task.done()}
            try:
                async with factory() as session:
                    expired = await expire_exhausted_runs(
                        session, max_attempts=self.settings.max_job_attempts
                    )
                    if expired:
                        LEASE_EXPIRATIONS.inc(expired)
                    depth = await session.scalar(
                        select(func.count()).select_from(AgentRun).where(
                            AgentRun.status == AgentRunStatus.PENDING.value
                        )
                    )
                    QUEUE_DEPTH.set(int(depth or 0))

                while (
                    len(self._active) < self.settings.worker_concurrency
                    and not self._stopping.is_set()
                ):
                    async with factory() as session:
                        claim = await claim_next_run(
                            session,
                            worker_id=self.worker_id,
                            lease_seconds=self.settings.job_lease_seconds,
                            max_attempts=self.settings.max_job_attempts,
                        )
                    if claim is None:
                        break
                    task = asyncio.create_task(self._execute_claim(claim))
                    self._active.add(task)
            except Exception:
                logger.exception("worker polling failed; retrying")

            if self._active:
                done, _ = await asyncio.wait(
                    self._active,
                    timeout=self.settings.worker_poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self._active.difference_update(done)
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.settings.worker_poll_seconds
                    )

        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)

    async def _execute_claim(self, claim: ClaimedRun) -> None:
        factory = get_session_factory()
        heartbeat = asyncio.create_task(self._heartbeat(claim.run_id))
        execution = asyncio.create_task(self._resume_or_execute(claim))
        started = time.perf_counter()
        ACTIVE_RUNS.inc()
        if claim.recover:
            RUN_RECOVERIES.inc()
            LEASE_EXPIRATIONS.inc()
        try:
            done, _ = await asyncio.wait(
                {execution, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                heartbeat.result()
                raise RuntimeError("lease ownership lost during execution")
            result = await execution
            RUNS.labels(status=str(result.get("status", "UNKNOWN"))).inc()
            async with factory() as session:
                await release_claim(
                    session, run_id=claim.run_id, worker_id=self.worker_id
                )
        except Exception as exc:
            logger.exception("worker execution failed for %s", claim.run_id)
            RUN_RETRIES.inc()
            delay = min(30.0, 2.0 ** max(0, claim.attempt_count - 1))
            async with factory() as session:
                await retry_claim(
                    session,
                    run_id=claim.run_id,
                    worker_id=self.worker_id,
                    max_attempts=self.settings.max_job_attempts,
                    failure_category=type(exc).__name__,
                    delay_seconds=delay,
                )
        finally:
            execution.cancel()
            heartbeat.cancel()
            await asyncio.gather(execution, heartbeat, return_exceptions=True)
            ACTIVE_RUNS.dec()
            RUN_DURATION.observe(time.perf_counter() - started)

    async def _resume_or_execute(self, claim: ClaimedRun) -> dict[str, object]:
        """Resume approval interrupts distinctly from ordinary checkpoint recovery."""
        from sqlalchemy import select

        from incidentpilot.agent.graph import resume_agent_workflow
        from incidentpilot.models.db import AgentRun, Approval
        from incidentpilot.models.enums import ApprovalDecision

        factory = get_session_factory()
        async with factory() as session:
            approval = (
                await session.execute(
                    select(Approval)
                    .join(AgentRun, Approval.run_pk == AgentRun.id)
                    .where(
                        AgentRun.run_id == claim.run_id,
                        Approval.decision == ApprovalDecision.APPROVE.value,
                    )
                )
            ).scalars().first()
        if approval is not None:
            return await resume_agent_workflow(
                run_id=claim.run_id,
                decision=ApprovalDecision.APPROVE.value,
                settings=self.settings,
            )
        return await self.executor.execute(
            incident_pk=claim.incident_pk,
            run_id=claim.run_id,
            recover=claim.recover,
        )

    async def _heartbeat(self, run_id: str) -> None:
        factory = get_session_factory()
        while True:
            await asyncio.sleep(self.settings.job_heartbeat_seconds)
            async with factory() as session:
                owned = await renew_lease(
                    session,
                    run_id=run_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.settings.job_lease_seconds,
                )
            if not owned:
                logger.warning("worker %s lost lease for %s", self.worker_id, run_id)
                return


async def _main() -> None:
    settings = get_settings()
    if not settings.database_url.startswith("postgresql"):
        raise RuntimeError("durable worker requires PostgreSQL")
    worker = DurableWorker(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)
    await worker.run()


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(_main())
    else:
        asyncio.run(_main())


if __name__ == "__main__":
    main()
