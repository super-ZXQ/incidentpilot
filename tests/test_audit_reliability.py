"""Fencing, terminal routing, cancellation and remote reconciliation regressions."""

import asyncio
import base64
import json
import os
import subprocess
import sys
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from incidentpilot.config import Settings
from incidentpilot.github.integration import GitHubIntegration
from incidentpilot.models.enums import RUN_TRANSITIONS, TERMINAL_RUN_STATUSES, AgentRunStatus
from incidentpilot.persistence.jobs import (
    claim_next_run,
    create_incident_and_run,
    renew_lease,
    utcnow,
)
from incidentpilot.persistence.ownership import LostLeaseError, current_claim
from incidentpilot.persistence.session import get_session_factory
from incidentpilot.sandbox.process import run_process
from incidentpilot.services.incidents import RunService


async def make_run(session):
    return await create_incident_and_run(session, incident_data={"title": "fence", "service": "orders-api", "symptom": "latency"}, max_queued_runs=100)


@pytest.mark.parametrize("same_owner", [True, False])
async def test_old_attempt_cannot_write_final_state(db_session, same_owner):
    _, run = await make_run(db_session)
    first = await claim_next_run(db_session, worker_id="a", lease_seconds=10, max_attempts=3)
    async with get_session_factory()() as stale_session:
        stale = await RunService(stale_session).get(run.run_id)
        # Release the read transaction before simulating a takeover.
        await stale_session.commit()
        second = await claim_next_run(db_session, worker_id="a" if same_owner else "b", lease_seconds=10, max_attempts=3, now=utcnow() + timedelta(seconds=11))
        assert second.attempt_count == 2
        token = current_claim.set(first)
        try:
            with pytest.raises(LostLeaseError):
                await RunService(stale_session).mark_status(stale, AgentRunStatus.FAILED, finished=True)
        finally:
            current_claim.reset(token)
    await db_session.refresh(run)
    assert run.status == "RUNNING"
    assert run.attempt_count == 2


async def test_expired_lease_cannot_be_revived(db_session):
    _, run = await make_run(db_session)
    first = await claim_next_run(db_session, worker_id="a", lease_seconds=10, max_attempts=3)
    assert not await renew_lease(db_session, run_id=run.run_id, worker_id="a", attempt_count=first.attempt_count, lease_seconds=10, now=utcnow() + timedelta(seconds=11))


@pytest.mark.parametrize("status", list(TERMINAL_RUN_STATUSES))
async def test_terminal_status_is_absorbing(db_session, status):
    _, run = await make_run(db_session)
    run.status = status.value
    await db_session.commit()
    assert RUN_TRANSITIONS[status] == {status}
    with pytest.raises(RuntimeError):
        await RunService(db_session).mark_status(run, AgentRunStatus.RUNNING)


async def test_needs_human_with_existing_evidence_stops_graph(db_session, monkeypatch):
    from incidentpilot.agent.decision import AgentDecisionModel
    from incidentpilot.agent.graph import run_agent_workflow
    from incidentpilot.llm.schemas import NextAction, ToolSelection

    incident, run = await make_run(db_session)
    actions = [NextAction(action="collect_evidence", tool_selection=ToolSelection(tool_name="read_metrics", arguments={"service": "orders-api"}, reason="telemetry")), NextAction(action="needs_human", reason="stop")]
    monkeypatch.setattr(AgentDecisionModel, "next_action", AsyncMock(side_effect=actions))
    result = await run_agent_workflow(incident_pk=incident.id, run_id=run.run_id, settings=Settings(database_url="sqlite+aiosqlite:///test", tool_backend="fake"))
    assert result["status"] == "NEEDS_HUMAN_INTERVENTION"
    assert len(result["tool_calls"]) == 1
    assert result["patch_attempts"] == []
    assert not result["root_cause"]


def process_alive(pid):
    if os.name == "nt":
        return str(pid) in subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_cancellation_reaps_process_and_keeps_loop_responsive(tmp_path):
    marker = tmp_path / "pid"
    code = "import os,time,pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(120)"
    task = asyncio.create_task(run_process([sys.executable, "-c", code], timeout=150))
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(.01)
    assert marker.exists()
    pid = int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not process_alive(pid)


class RemoteGitHub:
    def __init__(self, failure):
        self.base = "a" * 40
        self.branch_sha = None
        self.pr = None
        self.failure = failure
        self.fired = False
        self.writes = {"refs": 0, "pulls": 0}

    def __call__(self, request):
        path = request.url.path.split("/repos/test/repo", 1)[1]
        body = json.loads(request.content) if request.content else {}
        def response(data, code=200):
            return httpx.Response(code, json=data)
        if request.method == "GET":
            if path == "/pulls":
                return response([self.pr] if self.pr else [])
            if path == "/git/ref/heads/main":
                return response({"object": {"sha": self.base}})
            if path.startswith("/git/ref/heads/"):
                return response({"object": {"sha": self.branch_sha}}) if self.branch_sha else response({}, 404)
            if path == f"/git/commits/{self.base}":
                return response({"tree": {"sha": "base-tree"}, "parents": []})
            if path.startswith("/git/commits/"):
                return response({"tree": {"sha": "approved-tree"}, "parents": [{"sha": self.base}]})
            if path == "/git/trees/base-tree":
                return response({"tree": [{"path": "app.py", "mode": "100644", "type": "blob", "sha": "base-blob"}]})
            if path == "/git/blobs/base-blob":
                return response({"content": base64.b64encode(b"x\n").decode()})
        else:
            if self.failure in {"429", "503"} and not self.fired:
                self.fired = True
                return response({}, int(self.failure))
            if path == "/git/blobs":
                assert base64.b64decode(body["content"]) == b"y\n"
                return response({"sha": "fixed-blob"}, 201)
            if path == "/git/trees":
                assert body["tree"] == [{"path": "app.py", "mode": "100644", "type": "blob", "sha": "fixed-blob"}]
                return response({"sha": "approved-tree"}, 201)
            if path == "/git/commits":
                if self.failure == "commit" and not self.fired:
                    self.fired = True
                    raise httpx.ReadTimeout("crash after commit")
                return response({"sha": "approved-commit"}, 201)
            if path == "/git/refs":
                self.branch_sha = body["sha"]
                self.writes["refs"] += 1
                if self.failure == "branch" and not self.fired:
                    self.fired = True
                    raise httpx.ReadTimeout("crash after branch")
                return response({}, 201)
            if path == "/pulls":
                self.pr = {"html_url": "https://example.invalid/pr/1", "number": 1, "head": {"sha": self.branch_sha}}
                self.writes["pulls"] += 1
                if self.failure == "pr" and not self.fired:
                    self.fired = True
                    raise httpx.ReadTimeout("crash after PR")
                return response(self.pr, 201)
        raise AssertionError(f"unexpected HTTP {request.method} {path}")


@pytest.mark.parametrize("failure", ["branch", "commit", "pr", "429", "503"])
async def test_github_crash_timeout_and_http_retry_reconcile_remote(failure):
    remote = RemoteGitHub(failure)
    kwargs = dict(run_id="RUN-audit", title="fix", body="approved", patch_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n", base_commit_sha=remote.base, idempotency_key="stable")
    def integration():
        return GitHubIntegration(enabled=True, token="test-only", repo="test/repo", transport=httpx.MockTransport(remote))
    first = await integration().acreate_pull_request(**kwargs)
    assert not first.ok and first.details["retryable"]
    second = await integration().acreate_pull_request(**kwargs)
    assert second.ok
    third = await integration().acreate_pull_request(**kwargs)
    assert third.ok and third.pr_url == second.pr_url
    assert remote.writes == {"refs": 1, "pulls": 1}


async def test_github_wrong_base_fails_before_writes():
    remote = RemoteGitHub(None)
    integration = GitHubIntegration(enabled=True, token="test-only", repo="test/repo", transport=httpx.MockTransport(remote))
    result = await integration.acreate_pull_request(run_id="x", title="fix", body="b", patch_diff="diff", base_commit_sha="b" * 40)
    assert not result.ok
    assert "stale" in result.error
    assert remote.writes == {"refs": 0, "pulls": 0}


async def test_github_honors_server_settings_and_never_downgrades_enabled_mode():
    from incidentpilot.github.integration import default_github_integration

    configured = default_github_integration(Settings(github_integration_enabled=True, github_token="test-only", github_repo="test/repo"))
    assert configured.enabled is True
    missing = default_github_integration(Settings(github_integration_enabled=True, github_token="", github_repo=""))
    result = await missing.acreate_pull_request(run_id="test", title="t", body="b", patch_diff="diff", base_commit_sha="abc")
    assert not result.ok and result.mode == "real"



async def test_patch_retries_start_from_base_and_preserve_each_test_exit(db_session, tmp_path, monkeypatch):
    from sqlalchemy import select

    from incidentpilot.agent.graph import run_agent_workflow
    from incidentpilot.models.db import TestRun as SandboxTestRecord
    from incidentpilot.sandbox.manager import hash_patch

    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    (source / "app.py").write_bytes(b"VALUE = 0\n")
    (source / "tests" / "test_regression.py").write_bytes(b"from app import VALUE\ndef test_fixed_value():\n    assert VALUE == 2\n")
    patches = [json.dumps({"type": "simple_replace", "edits": [{"path": "app.py", "find": "VALUE = 0", "replace": f"VALUE = {number}"}]}) for number in (1, 2)]
    iterator = iter(patches)
    monkeypatch.setattr("incidentpilot.sandbox.manager.generate_deterministic_patch", lambda summary: next(iterator))
    incident, run = await make_run(db_session)
    settings = Settings(database_url="sqlite+aiosqlite:///test", tool_backend="fake", environment="test", sandbox_enabled=False, reference_repo_path=str(source), max_patch_attempts=2)
    result = await run_agent_workflow(incident_pk=incident.id, run_id=run.run_id, settings=settings)
    assert result["status"] == "WAITING_APPROVAL", result.get("error")
    assert result["patch_artifact"]["patch_hash"] == hash_patch(patches[1])
    assert [p["test_ok"] for p in result["patch_attempts"]] == [False, True]
    records = list((await db_session.execute(select(SandboxTestRecord).where(SandboxTestRecord.run_pk == run.id).order_by(SandboxTestRecord.created_at))).scalars())
    assert [(r.status, r.exit_code) for r in records] == [("FAILED", 1), ("PASSED", 0)]
