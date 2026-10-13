"""Opt-in failure injection against a dedicated disposable PostgreSQL database."""

import asyncio
import os
import sys
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import update

from incidentpilot.config import Settings
from incidentpilot.models.db import AgentRun
from incidentpilot.models.enums import AgentRunStatus
from incidentpilot.persistence.jobs import claim_next_run, create_incident_and_run, utcnow
from incidentpilot.persistence.session import (
    dispose_engine,
    get_session_factory,
    init_db,
    reset_db_state,
)
from incidentpilot.sandbox.process import run_process
from incidentpilot.services.incidents import RunService
from incidentpilot.worker import DurableWorker
from tests.test_audit_reliability import process_alive

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def audit_database(monkeypatch):
    url = os.environ.get("AUDIT_POSTGRES_URL")
    if not url:
        pytest.skip("AUDIT_POSTGRES_URL must identify a disposable test database")
    reset_db_state()
    monkeypatch.setenv("DATABASE_URL", url)
    await init_db(url)
    yield Settings(database_url=url, job_lease_seconds=10, job_heartbeat_seconds=1, tool_backend="fake", environment="test")
    await dispose_engine()
    reset_db_state()


async def claimed(settings):
    factory = get_session_factory()
    async with factory() as session:
        incident, run = await create_incident_and_run(session, incident_data={"title": "long heartbeat", "service": "orders-api", "symptom": "test-only"}, max_queued_runs=1000)
        claim = await claim_next_run(session, worker_id="audit-a", lease_seconds=10, max_attempts=3, run_id=run.run_id)
    return run, claim


async def test_long_subprocess_over_60s_keeps_heartbeat_and_excludes_second_worker(audit_database, monkeypatch):
    run, claim = await claimed(audit_database)
    worker = DurableWorker(audit_database, worker_id="audit-a")
    async def execute(_):
        code, _, _ = await run_process([sys.executable, "-c", "import time; time.sleep(62)"], timeout=70)
        assert code == 0
        async with get_session_factory()() as session:
            current = await RunService(session).get(run.run_id)
            await RunService(session).mark_status(current, AgentRunStatus.INVESTIGATION_COMPLETE, finished=True)
        return {"status": "INVESTIGATION_COMPLETE"}
    monkeypatch.setattr(worker, "_resume_or_execute", execute)
    task = asyncio.create_task(worker._execute_claim(claim))
    try:
        # The workload is real wall-clock time; no fake time acceleration.
        for _ in range(6):
            await asyncio.sleep(10.1)
            async with get_session_factory()() as session:
                current = await RunService(session).get(run.run_id)
                assert current.last_heartbeat_at >= utcnow() - timedelta(seconds=3)
                second = await claim_next_run(session, worker_id="audit-b", lease_seconds=10, max_attempts=3, run_id=run.run_id)
                assert second is None
        await asyncio.wait_for(task, timeout=10)
        async with get_session_factory()() as session:
            current = await RunService(session).get(run.run_id)
            assert current.status == "INVESTIGATION_COMPLETE"
            assert current.attempt_count == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_takeover_cancels_old_process_without_overwriting_new_owner(audit_database, monkeypatch, tmp_path):
    run, claim = await claimed(audit_database)
    worker = DurableWorker(audit_database, worker_id="audit-a")
    marker = tmp_path / "pid"
    async def execute(_):
        code = "import os,time,pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(120)"
        await run_process([sys.executable, "-c", code], timeout=150)
        pytest.fail("lost worker continued execution")
    monkeypatch.setattr(worker, "_resume_or_execute", execute)
    task = asyncio.create_task(worker._execute_claim(claim))
    try:
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(.03)
        assert marker.exists()
        async with get_session_factory()() as session:
            await session.execute(update(AgentRun).where(AgentRun.run_id == run.run_id).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
            await session.commit()
            second = await claim_next_run(session, worker_id="audit-b", lease_seconds=10, max_attempts=3, run_id=run.run_id)
        assert second.attempt_count == 2
        await asyncio.wait_for(task, timeout=10)
        assert not process_alive(int(marker.read_text()))
        async with get_session_factory()() as session:
            current = await RunService(session).get(run.run_id)
            assert current.worker_id == "audit-b"
            assert current.status == "RUNNING"
            assert current.attempt_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_database_heartbeat_error_cancels_work_before_retry(audit_database, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    run, claim = await claimed(audit_database)
    worker = DurableWorker(audit_database, worker_id="audit-a")
    marker = tmp_path / "disconnect-pid"
    async def execute(_):
        code = "import os,time,pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(120)"
        await run_process([sys.executable, "-c", code], timeout=150)
        pytest.fail("execution survived heartbeat connection failure")
    monkeypatch.setattr(worker, "_resume_or_execute", execute)
    monkeypatch.setattr("incidentpilot.worker.renew_lease", AsyncMock(side_effect=ConnectionError("injected DB disconnect")))
    await asyncio.wait_for(worker._execute_claim(claim), timeout=10)
    assert marker.exists() and not process_alive(int(marker.read_text()))
    async with get_session_factory()() as session:
        current = await RunService(session).get(run.run_id)
        assert current.status == "PENDING"
        assert current.failure_category == "ConnectionError"


async def test_postgres_checkpoint_rebuilds_sandbox_after_worker_cancellation(audit_database, monkeypatch):
    from pathlib import Path

    from incidentpilot.agent.graph import run_agent_workflow
    from incidentpilot.persistence.ownership import current_claim
    from incidentpilot.sandbox.manager import SandboxManager

    run, claim = await claimed(audit_database)
    settings = audit_database.model_copy(update={"reference_repo_path": str(Path(__file__).resolve().parents[1] / "reference" / "orders_api")})
    started = asyncio.Event()
    original = SandboxManager.run_tests_async
    async def cancelled_tests(self, *args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(SandboxManager, "run_tests_async", cancelled_tests)
    token = current_claim.set(claim)
    task = asyncio.create_task(run_agent_workflow(incident_pk=run.incident_pk, run_id=run.run_id, settings=settings))
    try:
        await asyncio.wait_for(started.wait(), timeout=15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        current_claim.reset(token)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with get_session_factory()() as session:
        await session.execute(update(AgentRun).where(AgentRun.run_id == run.run_id).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        await session.commit()
        second = await claim_next_run(session, worker_id="audit-b", lease_seconds=30, max_attempts=3, run_id=run.run_id)
    monkeypatch.setattr(SandboxManager, "run_tests_async", original)
    token = current_claim.set(second)
    try:
        result = await run_agent_workflow(incident_pk=run.incident_pk, run_id=run.run_id, settings=settings, recover=True)
    finally:
        current_claim.reset(token)
    assert result["status"] == "WAITING_APPROVAL", result.get("error")
    assert result["result"]["test_status"] == "PASSED"
    assert result["patch_artifact"]["base_commit_sha"] == result["patch_attempts"][-1]["base_commit_sha"]



def test_sql_executes_only_with_dedicated_readonly_role(monkeypatch):
    from ops_mcp.ops_readonly.server import query_database_readonly

    readonly = os.environ.get("AUDIT_REFERENCE_DB_URL")
    admin = os.environ.get("AUDIT_REFERENCE_ADMIN_URL")
    if not readonly or not admin:
        pytest.skip("dedicated reference audit database URLs not configured")
    monkeypatch.setenv("REFERENCE_DB_URL", admin)
    refused = query_database_readonly("SELECT id FROM orders")
    assert "readonly" in refused.get("error", "")
    monkeypatch.setenv("REFERENCE_DB_URL", readonly)
    accepted = query_database_readonly("SELECT id FROM orders")
    assert not accepted.get("error"), accepted
    assert accepted["rows"] == [{"id": 1}]
    for query in ("SELECT name FROM customers", "SELECT * FROM pg_catalog.pg_authid", "DELETE FROM orders"):
        assert query_database_readonly(query).get("error")
