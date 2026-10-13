"""Attempt fencing propagated through async tasks, never through model state."""

import hashlib
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import func, select, text, update

from incidentpilot.models.db import AgentRun
from incidentpilot.persistence.jobs import ClaimedRun, utcnow

current_claim: ContextVar[ClaimedRun | None] = ContextVar("current_claim", default=None)


class LostLeaseError(RuntimeError):
    pass


async def fence(session, run_id: str) -> None:
    claim = current_claim.get()
    if claim is None:
        owner = await session.scalar(select(AgentRun.worker_id).where(AgentRun.run_id == run_id))
        if owner is not None:
            raise LostLeaseError("leased run requires worker context")
        return
    if claim.run_id != run_id:
        raise LostLeaseError("claim belongs to another run")
    # The conditional UPDATE locks the row until the caller commits artifacts or
    # state, so another claimer cannot interleave a final write.
    current = func.clock_timestamp() if session.bind is not None and session.bind.dialect.name == "postgresql" else utcnow()
    result = await session.execute(update(AgentRun).where(
        AgentRun.run_id == run_id,
        AgentRun.worker_id == claim.worker_id,
        AgentRun.attempt_count == claim.attempt_count,
        AgentRun.lease_expires_at > current,
        AgentRun.status == "RUNNING",
    ).values(worker_id=claim.worker_id).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await session.rollback()
        raise LostLeaseError("lease expired or superseded")


@asynccontextmanager
async def workflow_lock(run_id: str, settings):
    """Serialize checkpoint writers for a run across PostgreSQL workers."""
    if not settings.database_url.startswith("postgresql"):
        yield
        return
    import asyncio

    from incidentpilot.persistence.session import get_session_factory

    key = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8], "big", signed=True)
    async with get_session_factory()() as session:
        acquired = False
        try:
            while not acquired:
                acquired = bool(await session.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
                if not acquired:
                    await session.rollback()
                    await asyncio.sleep(0.1)
            # Keep this connection pinned until unlock; do not return a locked
            # connection to the pool via commit/rollback.
            yield
        finally:
            if acquired:
                await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            await session.rollback()
