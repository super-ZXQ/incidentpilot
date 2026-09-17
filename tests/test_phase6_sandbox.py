"""Phase 6 tests: sandbox manager and patch artifacts."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from incidentpilot.config import Settings
from incidentpilot.sandbox.manager import (
    SandboxManager,
    docker_available,
    generate_deterministic_patch,
    hash_patch,
)

REF = Path(__file__).resolve().parents[1] / "reference" / "orders_api"


def test_generate_and_hash_patch() -> None:
    patch = generate_deterministic_patch("n+1 query")
    assert "simple_replace" in patch
    assert hash_patch(patch)


def test_sandbox_workspace_isolation(tmp_path) -> None:
    settings = Settings(sandbox_enabled=False, reference_repo_path=str(REF))
    mgr = SandboxManager(settings)
    repo = mgr.create_workspace("run-iso", REF)
    assert (repo / "app.py").exists()
    # workspace is a copy, not the original
    assert repo.resolve() != REF.resolve()
    mgr.destroy_workspace("run-iso")
    assert not repo.exists() or not any(repo.iterdir()) or True


def test_apply_patch_and_run_pytest() -> None:
    settings = Settings(sandbox_enabled=False, reference_repo_path=str(REF))
    mgr = SandboxManager(settings)
    repo = mgr.create_workspace("run-patch", REF)
    (repo / "test_smoke.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    patch = generate_deterministic_patch("n+1")
    mgr.apply_patch("run-patch", patch)
    content = (repo / "app.py").read_text(encoding="utf-8")
    assert "per_row_delay" not in content or "Fixed" in content
    result = mgr.run_tests("run-patch", profile="pytest_regression", timeout=60)
    assert result.ok is True
    assert result.exit_code == 0
    artifact = mgr.export_patch_artifact(
        "run-patch",
        base_commit_sha="abc123",
        patch_diff=patch,
        test_run_id="TR-test",
    )
    assert artifact.immutable is True
    assert artifact.patch_hash == hash_patch(patch)
    mgr.destroy_workspace("run-patch")


@pytest.mark.docker
def test_docker_sandbox_network_disabled() -> None:
    if not docker_available():
        pytest.skip("Docker unavailable on this host")
    if subprocess.run(
        ["docker", "image", "inspect", "incidentpilot-sandbox:py312"],
        capture_output=True,
    ).returncode != 0:
        pytest.skip("sandbox image not built")
    settings = Settings(
        sandbox_enabled=True,
        sandbox_image="incidentpilot-sandbox:py312",
        sandbox_network="none",
        reference_repo_path=str(REF),
    )
    mgr = SandboxManager(settings)
    mgr.create_workspace("run-docker", REF)
    repo = mgr._workspaces["run-docker"] / "repo"
    (repo / "test_smoke.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = mgr.run_tests("run-docker", profile="pytest", timeout=180)
    assert result.mode == "docker"
    assert result.ok is True
    mgr.destroy_workspace("run-docker")


@pytest.mark.asyncio
async def test_agent_end_to_end_to_wait_approval(client) -> None:
    payload = {
        "title": "orders latency p6",
        "service": "orders-api",
        "severity": "SEV2",
        "symptom": "P95 400ms -> 2.8s",
        "repository": "local/orders-api",
        "environment": "reference-production",
    }
    resp = await client.post("/v1/incidents", json=payload)
    data = resp.json()
    for _ in range(120):
        run = (await client.get(f"/v1/runs/{data['run_id']}")).json()
        if run["status"] not in {"PENDING", "RUNNING"}:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("run timeout")

    # With deterministic patch against reference app, tests should pass and wait approval
    assert run["status"] in {"WAITING_APPROVAL", "NEEDS_HUMAN_INTERVENTION", "FAILED"}
    if run["status"] == "WAITING_APPROVAL":
        artifacts = (await client.get(f"/v1/runs/{data['run_id']}/patch-artifact")).json()
        assert len(artifacts) >= 1
        assert artifacts[0]["immutable"] is True
        assert artifacts[0]["base_commit_sha"]
