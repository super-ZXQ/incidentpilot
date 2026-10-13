"""Behavioral Docker security probes in ephemeral test-only workspaces."""

import asyncio
import subprocess

import pytest

from incidentpilot.config import Settings
from incidentpilot.sandbox.manager import SandboxManager, docker_available

pytestmark = pytest.mark.docker


def require_image():
    if not docker_available():
        pytest.skip("Docker daemon unavailable")
    if subprocess.run(["docker", "image", "inspect", "incidentpilot-sandbox:py312"], capture_output=True).returncode:
        pytest.skip("sandbox image not built")


async def test_container_cannot_access_host_secrets_socket_or_network(tmp_path):
    require_image()
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text("SECRET=test-only")
    (tmp_path / "host-sensitive.txt").write_text("test-only marker")
    (source / "test_security.py").write_text("""
import os
import socket
from pathlib import Path
import pytest

def test_security_boundary():
    assert os.getuid() == 65534
    assert not Path('/var/run/docker.sock').exists()
    assert not Path('/workspace/.env').exists()
    assert not Path('/host-sensitive.txt').exists()
    status = Path('/proc/self/status').read_text()
    assert 'NoNewPrivs:   1' in status.replace('\\t', '   ')
    assert int(next(line.split(':')[1].strip() for line in status.splitlines() if line.startswith('CapEff:')), 16) == 0
    with pytest.raises(OSError):
        Path('/workspace/unapproved.py').write_text('tampered')
    with pytest.raises(OSError):
        Path('/etc/unsafe').write_text('tampered')
    for address in [('1.1.1.1', 443), ('172.17.0.1', 2375)]:
        with pytest.raises(OSError):
            socket.create_connection(address, timeout=.3)
""", encoding="utf-8")
    manager = SandboxManager(Settings(environment="test", sandbox_enabled=True))
    try:
        manager.create_workspace("docker-security", source)
        result = await manager.run_tests_async("docker-security", timeout=30)
        assert result.ok, result.stdout + result.stderr
        assert result.mode == "docker"
    finally:
        manager.destroy_workspace("docker-security")


async def test_cancelling_docker_test_removes_running_container(tmp_path):
    require_image()
    (tmp_path / "test_long.py").write_text("import time\ndef test_long(): time.sleep(120)\n")
    manager = SandboxManager(Settings(environment="test", sandbox_enabled=True))
    manager.create_workspace("docker-cancel", tmp_path)
    task = asyncio.create_task(manager.run_tests_async("docker-cancel", timeout=150))
    from incidentpilot.sandbox.process import run_process
    async def containers():
        _, output, _ = await run_process(["docker", "ps", "-aq", "--filter", "label=incidentpilot.run_id=docker-cancel"], timeout=10)
        return output.strip()
    try:
        for _ in range(50):
            if await containers():
                break
            await asyncio.sleep(.1)
        assert await containers()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await containers()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        manager.destroy_workspace("docker-cancel")
