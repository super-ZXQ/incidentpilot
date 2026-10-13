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
    if tamper == "diff":
        artifact.patch_diff += "tamper"
    elif tamper == "base":
        artifact.base_commit_sha = "changed"
    elif tamper == "test":
        artifact.test_run_id = "unknown"
    elif tamper == "run":
        run.result = {"patch_artifact_id": "another-run"}
    else:
        artifact.patch_hash = "0" * 64
    await db_session.commit()
    with pytest.raises(ApprovalConflict):
        await ApprovalService(db_session).decide(run, ApprovalDecision.APPROVE, actor="admin")


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
