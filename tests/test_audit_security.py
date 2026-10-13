"""Regression tests for audited API, artifact and sandbox boundaries."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from incidentpilot.api.app import create_app
from incidentpilot.config import Settings
from incidentpilot.models.db import PatchArtifact
from incidentpilot.models.db import TestRun as SandboxTestRecord
from incidentpilot.models.enums import AgentRunStatus, ApprovalDecision
from incidentpilot.persistence.jobs import create_incident_and_run
from incidentpilot.sandbox.manager import SandboxManager, hash_patch, patch_paths
from incidentpilot.services.incidents import ApprovalConflict, ApprovalService

TOKENS = {role: {"subject": role, "role": role} for role in ("admin", "initiator", "reviewer", "approver")}


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/incidents"), ("GET", "/v1/incidents/unknown"),
    ("GET", "/v1/runs/unknown"), ("GET", "/v1/runs/unknown/evidence"),
    ("GET", "/v1/runs/unknown/tool-calls"), ("GET", "/v1/runs/unknown/patch-artifact"),
    ("POST", "/v1/runs/unknown/approval"), ("GET", "/metrics"),
    ("GET", "/v1/runtime"), ("GET", "/v1/runs/unknown/tests"), ("GET", "/v1/runs/unknown/approval"),
])
async def test_all_protected_routes_require_identity(db_session, method, path):
    app = create_app(Settings(api_tokens=TOKENS))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json={"decision": "APPROVE"} if "approval" in path else {"title": "x", "service": "x", "symptom": "x"})
        assert response.status_code == 401
        assert (await client.get("/health")).status_code == 200
        assert (await client.request(method, path, headers={"Authorization": "Bearer invalid"}, json={"decision": "APPROVE"} if "approval" in path else {"title": "x", "service": "x", "symptom": "x"})).status_code == 401


async def test_roles_ownership_and_approval_actor(db_session):
    app = create_app(Settings(api_tokens=TOKENS, embedded_worker_enabled=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"title": "x", "service": "orders-api", "symptom": "x"}
        for role in ("reviewer", "approver"):
            assert (await client.post("/v1/incidents", json=body, headers={"Authorization": f"Bearer {role}"})).status_code == 403
        created = await client.post("/v1/incidents", json=body, headers={"Authorization": "Bearer admin"})
        run_id = created.json()["run_id"]
        assert (await client.get(f"/v1/runs/{run_id}", headers={"Authorization": "Bearer initiator"})).status_code == 403
        assert (await client.get(f"/v1/runs/{run_id}", headers={"Authorization": "Bearer reviewer"})).status_code == 200
        assert (await client.post(f"/v1/runs/{run_id}/approval", json={"decision": "APPROVE", "actor": "admin"}, headers={"Authorization": "Bearer initiator"})).status_code == 403


async def test_non_ascii_invalid_bearer_is_rejected_without_server_error(db_session):
    app = create_app(Settings(api_tokens=TOKENS))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/runtime", headers={"Authorization": b"Bearer \xff"})
    assert response.status_code == 401


async def make_artifact(session):
    _, run = await create_incident_and_run(session, incident_data={"title": "artifact", "service": "orders-api", "symptom": "latency"}, max_queued_runs=10)
    patch = json.dumps({"type": "simple_replace", "edits": [{"path": "app.py", "find": "x", "replace": "y"}]})
    artifact = PatchArtifact(patch_artifact_id="PA-audit", run_pk=run.id, base_commit_sha="abc", patch_diff=patch, patch_hash=hash_patch(patch), test_run_id="TR-audit")
    session.add_all([artifact, SandboxTestRecord(test_run_id="TR-audit", run_pk=run.id, status="PASSED", exit_code=0, patch_hash=hash_patch(patch), base_commit_sha="abc")])
    run.status = AgentRunStatus.WAITING_APPROVAL.value
    run.result = {"patch_artifact_id": artifact.patch_artifact_id}
    await session.commit()
    return run, artifact


async def test_approval_binding_and_trusted_actor(db_session):
    run, artifact = await make_artifact(db_session)
    app = create_app(Settings(api_tokens=TOKENS))
    # Only inspect persisted decision; graph resume is separately covered.
    from unittest.mock import AsyncMock, patch
    with patch("incidentpilot.agent.graph.resume_agent_workflow", new=AsyncMock(return_value={"status": "RUNNING"})):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/v1/runs/{run.run_id}/approval", headers={"Authorization": "Bearer admin"}, json={"decision": "APPROVE", "actor": "forged"})
    assert response.status_code == 200
    assert response.json()["actor"] == "admin"
    assert response.json()["patch_artifact_id"] == artifact.patch_artifact_id


@pytest.mark.parametrize("tamper", ["diff", "test", "base", "run", "hash"])
async def test_tampered_artifacts_cannot_be_approved(db_session, tamper):
    run, artifact = await make_artifact(db_session)
    # Simulate an out-of-band storage corruption, bypassing ORM immutability.
    from sqlalchemy import update

    values = {}
    if tamper == "diff":
        values["patch_diff"] = artifact.patch_diff + "tamper"
    elif tamper == "base":
        values["base_commit_sha"] = "changed"
    elif tamper == "test":
        values["test_run_id"] = "unknown"
    elif tamper == "run":
        run.result = {"patch_artifact_id": "another-run"}
    else:
        values["patch_hash"] = "0" * 64
    if values:
        await db_session.execute(update(PatchArtifact).where(PatchArtifact.id == artifact.id).values(**values))
    await db_session.commit()
    with pytest.raises(ApprovalConflict):
        await ApprovalService(db_session).decide(run, ApprovalDecision.APPROVE, actor="admin")


async def test_orm_artifact_cannot_be_modified(db_session):
    _, artifact = await make_artifact(db_session)
    artifact.patch_diff = "changed"
    with pytest.raises(ValueError, match="immutable"):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.parametrize("name", [".env", ".env.production", "api_token.txt", "credentials.json", "private/key.pem", ".aws/config"])
def test_sensitive_files_are_excluded(tmp_path, name):
    source = tmp_path / "source"
    target = source / name
    target.parent.mkdir(parents=True)
    target.write_text("sensitive")
    (source / "app.py").write_text("x = 1")
    manager = SandboxManager()
    try:
        copied = manager.create_workspace("copy", source)
        assert not (copied / name).exists()
        assert (copied / "app.py").exists()
    finally:
        manager.destroy_workspace("copy")


async def test_local_sandbox_keeps_system_drive_without_exposing_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setenv("INCIDENTPILOT_TEST_SECRET", "must-not-reach-child")
    (tmp_path / "test_environment.py").write_text(
        "import os\n"
        "def test_environment():\n"
        "    assert os.environ['SystemDrive'] == 'C:'\n"
        "    assert 'INCIDENTPILOT_TEST_SECRET' not in os.environ\n",
        encoding="utf-8",
    )
    manager = SandboxManager(Settings(environment="test", sandbox_enabled=False))
    try:
        manager.create_workspace("system-drive", tmp_path)
        result = await manager.run_tests_async("system-drive")
        assert result.ok, result.stdout + result.stderr
    finally:
        manager.destroy_workspace("system-drive")


async def test_protected_environment_never_runs_local(tmp_path):
    (tmp_path / "app.py").write_text("x = 1")
    manager = SandboxManager(Settings(environment="production", sandbox_enabled=False))
    try:
        manager.create_workspace("protected", tmp_path)
        with pytest.raises(RuntimeError, match="requires Docker"):
            await manager.run_tests_async("protected")
    finally:
        manager.destroy_workspace("protected")


@pytest.mark.parametrize("metadata", ["old mode 100644\nnew mode 100755", "new file mode 120000", "rename from app.py\nrename to other.py", "GIT binary patch"])
def test_unsupported_diff_metadata_is_rejected(tmp_path, metadata):
    patch = f"diff --git a/app.py b/app.py\n{metadata}\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(ValueError):
        patch_paths(tmp_path, patch)


@pytest.mark.parametrize("path", ["../x", "/tmp/x", "C:/x", "a/../../x", ".git/config", "file:stream", "app.py ", '"bad"'])
def test_special_patch_paths_rejected(tmp_path, path):
    patch = json.dumps({"type": "simple_replace", "edits": [{"path": path, "find": "x", "replace": "y"}]})
    with pytest.raises(ValueError):
        patch_paths(tmp_path, patch)


@pytest.mark.parametrize("operation", ["modify", "create", "delete"])
def test_real_git_diff_changes_match_declared_paths(tmp_path, operation):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_bytes(b"x\n")
    headers = {"modify": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n", "create": "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+y\n", "delete": "--- a/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"}
    manager = SandboxManager()
    try:
        copied = manager.create_workspace("diff", source)
        manager.apply_patch("diff", headers[operation])
        if operation == "delete":
            assert not (copied / "app.py").exists()
        else:
            assert (copied / ("new.py" if operation == "create" else "app.py")).read_text() == "y\n"
    finally:
        manager.destroy_workspace("diff")



async def test_missing_approval_checkpoint_stops_instead_of_retrying(db_session):
    from incidentpilot.persistence.jobs import ClaimedRun
    from incidentpilot.services.incidents import RunService
    from incidentpilot.worker import DurableWorker

    run, _ = await make_artifact(db_session)
    _, run = await ApprovalService(db_session).decide(run, ApprovalDecision.APPROVE, actor="test-admin", owner_id="missing-checkpoint-test")
    claim = ClaimedRun(run.incident_pk, run.run_id, run.worker_id, run.attempt_count, True)
    await DurableWorker(Settings(database_url="sqlite+aiosqlite:///test", tool_backend="fake"), worker_id=claim.worker_id)._execute_claim(claim)
    current = await RunService(db_session).get(run.run_id)
    await db_session.refresh(current)
    assert current.status == "NEEDS_HUMAN_INTERVENTION"
    assert "checkpoint missing" in current.error
    assert not current.result.get("pull_request")
