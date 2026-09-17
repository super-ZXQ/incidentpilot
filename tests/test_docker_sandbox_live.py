"""Real Docker sandbox isolation tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from incidentpilot.config import Settings
from incidentpilot.sandbox.manager import SandboxManager, docker_available

REF = Path(__file__).resolve().parents[1] / "reference" / "orders_api"


@pytest.mark.docker
def test_docker_sandbox_runs_pytest_network_none() -> None:
    if not docker_available():
        pytest.skip("Docker daemon unavailable")
    if subprocess.run(
        ["docker", "image", "inspect", "incidentpilot-sandbox:py312"],
        capture_output=True,
    ).returncode != 0:
        pytest.skip("build incidentpilot-sandbox:py312 before live Docker verification")

    settings = Settings(
        sandbox_enabled=True,
        sandbox_image="incidentpilot-sandbox:py312",
        sandbox_network="none",
        reference_repo_path=str(REF),
    )
    mgr = SandboxManager(settings)
    try:
        mgr.create_workspace("run-docker-live", REF)
        root = mgr._workspaces["run-docker-live"] / "repo"
        # install-free smoke test file that does not need project deps
        (root / "test_sandbox_smoke.py").write_text(
            "def test_sandbox_ok():\n    assert 2 + 2 == 4\n",
            encoding="utf-8",
        )
        result = mgr.run_tests("run-docker-live", profile="pytest", timeout=180)
        assert result.mode == "docker"
        assert result.ok is True
        assert result.command[0] == "docker"
        assert "--network" in result.command
        assert "none" in result.command
        assert "--user" in result.command
        assert "--cap-drop" in result.command
        # isolation: workspace is not the original path
        assert root.resolve() != REF.resolve()
    finally:
        mgr.destroy_workspace("run-docker-live")
        assert "run-docker-live" not in mgr._workspaces


@pytest.mark.docker
def test_docker_sandbox_security_flags_present() -> None:
    if not docker_available():
        pytest.skip("Docker daemon unavailable")
    settings = Settings(sandbox_enabled=True, sandbox_network="none")
    mgr = SandboxManager(settings)
    assert mgr is not None
    # Inspect constructed command via source constants
    source = Path(
        __file__
    ).resolve().parents[1] / "src" / "incidentpilot" / "sandbox" / "manager.py"
    text = source.read_text(encoding="utf-8")
    for token in [
        "--network",
        "--cap-drop",
        "ALL",
        "no-new-privileges",
        "--memory",
        "--cpus",
        "--pids-limit",
    ]:
        assert token in text
    assert "docker.sock" not in text or "cannot access Docker socket" in text
