"""Sandbox Manager: isolated patch application and test execution.

Agent never gets an unrestricted shell. Commands come from server-side policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incidentpilot.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Server-side allowlisted test commands (not LLM-generated)
ALLOWED_TEST_COMMANDS = {
    "pytest": ["python", "-m", "pytest", "-q", "--tb=short"],
    "pytest_fault": ["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", "../checks/test_fault_active.py"],
    # Regression profile: only healthy-path tests after a fix is applied
    "pytest_regression": [
        "python",
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "tests/test_regression.py",
    ],
}

FORBIDDEN_PATCH_PARTS = {
    ".git",
    ".env",
    "credentials",
    "credential",
    "secrets",
    "secret",
    "docker.sock",
    "conftest.py",
    ".gitattributes",
}
# The Docker socket cannot access Docker socket from the sandbox because it is
# never mounted; the name is also denied as a patch target.


def sensitive_path(raw_path: str) -> bool:
    parts = raw_path.replace("\\", "/").lower().split("/")
    return any(
        part in FORBIDDEN_PATCH_PARTS | {".ssh", ".aws", ".azure", ".config", ".github", ".venv", "venv", "node_modules"}
        or part.startswith(".env") or "credential" in part or "secret" in part
        or "token" in part or part.endswith((".pem", ".key", ".p12", ".pfx"))
        or part in {"id_rsa", "id_ed25519", "config.local", "private", ".netrc", ".npmrc", ".pypirc"}
        for part in parts
    )


def validate_patch_path(repo: Path, raw_path: str) -> Path:
    """Reject ambiguous Git/Windows paths and all symlink components."""
    normalized = raw_path.replace("\\", "/")
    if (not normalized or normalized.startswith(("/", "~")) or ":" in normalized
            or any(part in {"", ".", ".."} for part in normalized.split("/"))
            or any(ord(c) < 32 for c in normalized) or '"' in normalized):
        raise ValueError(f"unsafe patch path: {raw_path}")
    if sensitive_path(normalized):
        raise ValueError(f"forbidden patch path: {raw_path}")
    candidate = repo
    for part in normalized.split("/"):
        if part.rstrip(" .") != part:
            raise ValueError("unsafe Windows path alias")
        if part.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", "CLOCK$", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}:
            raise ValueError("unsafe Windows device path")
        candidate = candidate / part
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("symlink patch path forbidden")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(repo.resolve()):
        raise ValueError(f"patch path outside repository: {raw_path}")
    return resolved


def patch_paths(repo: Path, patch_diff: str) -> set[str]:
    """Git parses hunks and paths; unsupported metadata is rejected explicitly."""
    import json

    if len(patch_diff.encode("utf-8", errors="strict")) > 1_000_000 or "\x00" in patch_diff:
        raise ValueError("invalid or oversized patch")
    try:
        data = json.loads(patch_diff)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and data.get("type") == "simple_replace":
        edits = data.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ValueError("patch must contain at least one edit")
        paths = set()
        for item in edits:
            if (not isinstance(item, dict) or set(item) != {"path", "find", "replace"}
                    or not all(isinstance(v, str) for v in item.values()) or not item["find"]):
                raise ValueError("invalid simple patch edit")
            validate_patch_path(repo, item["path"])
            paths.add(item["path"])
        return paths
    for line in patch_diff.splitlines():
        if line.startswith(("rename ", "copy ", "similarity ", "dissimilarity ", "old mode ",
                            "new mode ", "GIT binary patch", "Binary files ")):
            raise ValueError("unsupported patch metadata")
        if line.startswith(("new file mode ", "deleted file mode ")) and not line.endswith("100644"):
            raise ValueError("unsupported file mode")
        if line.startswith("index ") and len(line.split()) > 2 and line.split()[-1] != "100644":
            raise ValueError("unsupported index mode")
    parsed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--numstat", "-z", "-"], input=patch_diff.encode("utf-8"),
        cwd=repo, capture_output=True, timeout=10,
    )
    if parsed.returncode or not parsed.stdout:
        raise ValueError("invalid or empty unified diff")
    paths = set()
    for record in parsed.stdout.decode("utf-8", errors="strict").rstrip("\x00").split("\x00"):
        added, removed, path = record.split("\t", 2)
        if not added.isdigit() or not removed.isdigit():
            raise ValueError("unsupported binary patch")
        validate_patch_path(repo, path)
        paths.add(path)
    return paths


def validate_patch_diff(repo: Path, patch_diff: str) -> None:
    patch_paths(repo, patch_diff)


def workspace_snapshot(repo: Path) -> dict[str, str]:
    snapshot = {}
    for path in repo.rglob("*"):
        if path.is_symlink():
            raise ValueError("workspace contains a symlink")
        if path.is_file():
            snapshot[path.relative_to(repo).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


@dataclass
class SandboxResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    command: list[str] = field(default_factory=list)
    mode: str = "local"


@dataclass
class PatchArtifactData:
    patch_artifact_id: str
    base_commit_sha: str
    patch_diff: str
    patch_hash: str
    test_run_id: str
    created_at: str
    immutable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_artifact_id": self.patch_artifact_id,
            "base_commit_sha": self.base_commit_sha,
            "patch_diff": self.patch_diff,
            "patch_hash": self.patch_hash,
            "test_run_id": self.test_run_id,
            "created_at": self.created_at,
            "immutable": self.immutable,
        }


def hash_patch(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


class SandboxManager:
    """Creates ephemeral workspace, applies patch, runs allowlisted tests."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._workspaces: dict[str, Path] = {}
        self._base_commits: dict[str, str] = {}

    def create_workspace(self, run_id: str, source_path: str | Path) -> Path:
        source = Path(source_path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"source path not found: {source}")
        self.destroy_workspace(run_id)
        root = Path(tempfile.mkdtemp(prefix=f"incidentpilot-{run_id[:8]}-"))
        dest = root / "repo"
        self._workspaces[run_id] = root
        def excluded(directory, names):
            return [name for name in names if sensitive_path(name)
                    or name in {"__pycache__", "var", ".pytest_cache"}
                    or name.endswith((".db", ".log")) or (Path(directory) / name).is_symlink() or (Path(directory) / name).is_junction()]
        if source.is_dir():
            shutil.copytree(source, dest, ignore=excluded)
        else:
            dest.mkdir(parents=True)
            if source.is_symlink() or sensitive_path(source.name):
                raise ValueError("sensitive source forbidden")
            shutil.copy2(source, dest / source.name)
        self._workspaces[run_id] = root
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source if source.is_dir() else source.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self._base_commits[run_id] = (
            commit.stdout.strip() if commit.returncode == 0 else "UNVERSIONED"
        )
        if commit.returncode == 0:
            dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=source if source.is_dir() else source.parent, capture_output=True, timeout=10)
            if dirty.stdout:
                self._base_commits[run_id] = "UNVERSIONED"
                if self.settings.environment not in {"development", "test"}:
                    raise ValueError("protected sandbox source must match a clean Git base")
        if self.settings.sandbox_fault_type:
            from incidentpilot.benchmarks.fault_cases import REGISTERED_FAULT_TYPES

            if self.settings.sandbox_fault_type not in REGISTERED_FAULT_TYPES:
                raise ValueError("unknown fault oracle")
            checks = root / "checks"
            checks.mkdir()
            (checks / "test_fault_active.py").write_text(
                "import time\nfrom app import create_app\nfrom faults import reset_fault\nfrom fastapi.testclient import TestClient\n"
                "def test_fault_active_restores_healthy_behavior():\n"
                "    reset_fault()\n    client = TestClient(create_app('sqlite:///:memory:'))\n"
                "    healthy = client.get('/orders').json()\n"
                f"    client.post('/admin/fault', json={{'fault_type': {self.settings.sandbox_fault_type!r}, 'params': {{'delay_seconds': .4, 'per_row_delay': .06}}}})\n"
                "    started = time.perf_counter()\n    response = client.get('/orders')\n"
                "    elapsed = time.perf_counter() - started\n"
                "    assert response.status_code == 200\n    assert response.json() == healthy\n"
                "    assert elapsed < .25, f'fault still active: {elapsed}s'\n    reset_fault()\n",
                encoding="utf-8",
            )
        return dest

    def base_commit_sha(self, run_id: str) -> str:
        return self._base_commits.get(run_id, "UNVERSIONED")

    def destroy_workspace(self, run_id: str) -> None:
        root = self._workspaces.pop(run_id, None)
        self._base_commits.pop(run_id, None)
        if root is not None and root.exists():
            shutil.rmtree(root, ignore_errors=True)

    def apply_patch(self, run_id: str, patch_diff: str) -> Path:
        root = self._workspaces.get(run_id)
        if root is None:
            raise RuntimeError("workspace not created")
        repo = root / "repo"
        paths = patch_paths(repo, patch_diff)
        before = workspace_snapshot(repo)
        import json

        if patch_diff.lstrip().startswith("{"):
            for item in json.loads(patch_diff)["edits"]:
                target = validate_patch_path(repo, item["path"])
                text = target.read_text(encoding="utf-8")
                if item["find"] not in text:
                    raise ValueError(f"find string not present in {item['path']}")
                target.write_text(text.replace(item["find"], item["replace"], 1), encoding="utf-8")
        else:
            for args in (["--check"], []):
                subprocess.run(
                    ["git", "-c", "core.autocrlf=false", "apply", *args, "-"], input=patch_diff.encode("utf-8"), cwd=repo,
                    capture_output=True, timeout=30, check=True,
                )
        after = workspace_snapshot(repo)
        changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        if changed != paths:
            raise ValueError("actual patch changes differ from declared paths")
        return repo

    def run_tests(self, run_id: str, profile: str = "pytest", timeout: int = 120) -> SandboxResult:
        """Synchronous compatibility entry point; Agent uses run_tests_async."""
        return asyncio.run(self.run_tests_async(run_id, profile, timeout))

    async def run_tests_async(self, run_id: str, profile: str = "pytest", timeout: int = 120) -> SandboxResult:
        from incidentpilot.sandbox.process import run_process

        if profile not in ALLOWED_TEST_COMMANDS:
            raise ValueError(f"test profile not allowlisted: {profile}")
        root = self._workspaces.get(run_id)
        if root is None:
            raise RuntimeError("workspace not created")
        repo = root / "repo"
        cmd = list(ALLOWED_TEST_COMMANDS[profile])
        if profile == "pytest_fault" and not (root / "checks" / "test_fault_active.py").is_file():
            raise ValueError("fault-active test oracle is missing")
        protected = self.settings.environment not in {"development", "test"}
        if protected and not self.settings.sandbox_enabled:
            raise RuntimeError("protected environment requires Docker sandbox")
        started = time.perf_counter()
        container = "ip-sandbox-" + uuid.uuid4().hex
        env = None
        if self.settings.sandbox_enabled:
            if self.settings.sandbox_network != "none":
                raise ValueError("sandbox network must be none")
            cmd = [
                "docker", "run", "--rm", "--name", container, "--label", f"incidentpilot.run_id={run_id}",
                "--network", "none", "--user", "65534:65534", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--memory", "512m", "--cpus", "1.0",
                "--pids-limit", "128", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTEST_ADDOPTS=-p no:cacheprovider",
                "-e", "PYTHONPATH=/workspace",
                *(["-v", f"{root / 'checks'}:/checks:ro"] if profile == "pytest_fault" else []),
                "-v", f"{repo}:/workspace:ro", "-w", "/workspace", self.settings.sandbox_image, *cmd,
            ]
        else:
            warnings.warn("NON-ISOLATED local sandbox: development/test only", RuntimeWarning, stacklevel=2)
            import sys

            cmd[0] = sys.executable
            allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
            env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
            env["PYTHONPATH"] = str(repo)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
        before_tests = workspace_snapshot(repo)
        try:
            code, stdout, stderr = await run_process(cmd, cwd=repo, env=env, timeout=timeout)
            if workspace_snapshot(repo) != before_tests:
                raise ValueError("test execution changed approved workspace content")
            return SandboxResult(
                ok=code == 0, exit_code=code, stdout=stdout, stderr=stderr,
                duration_ms=(time.perf_counter() - started) * 1000,
                command=cmd, mode="docker" if self.settings.sandbox_enabled else "local",
            )
        finally:
            if self.settings.sandbox_enabled:
                # Killing the Docker CLI does not stop its container.
                await run_process(["docker", "rm", "-f", container], timeout=15)

    def export_patch_artifact(
        self,
        run_id: str,
        *,
        base_commit_sha: str,
        patch_diff: str,
        test_run_id: str,
        created_at: str | None = None,
    ) -> PatchArtifactData:
        from datetime import UTC, datetime

        return PatchArtifactData(
            patch_artifact_id=f"PA-{uuid.uuid4().hex[:12]}",
            base_commit_sha=base_commit_sha,
            patch_diff=patch_diff,
            patch_hash=hash_patch(patch_diff),
            test_run_id=test_run_id,
            created_at=created_at or datetime.now(UTC).isoformat(),
            immutable=True,
        )


def generate_deterministic_patch(root_cause_summary: str, repo_name: str = "orders-api") -> str:
    """Test-only deterministic patch generator for CI fixtures.

    Produces a simple_replace patch that removes pathological per-row delay
    from the reference orders_api app.py listing path.
    """
    import json

    # Matches reference/orders_api/app.py after Phase 9 rewrite
    find_old = (
        '            if ftype in {"n_plus_one_query", "missing_index", "bad_query_refactor", "cache_failure"}:\n'
        '                time.sleep(float(params.get("per_row_delay", 0.03)))\n'
    )
    replace_new = (
        "            # Fixed: remove per-row delay after batching item loads\n"
        "            # (n_plus_one_query / missing_index / bad_query_refactor / cache_failure)\n"
    )
    # Also neutralize slow dependency delay for slow_database_query / dependency_timeout
    find_slow = (
        '        if ftype in {"slow_database_query", "dependency_timeout", "connection_pool_exhaustion"}:\n'
        '            time.sleep(float(params.get("delay_seconds", 0.5)))\n'
    )
    replace_slow = (
        "        # Fixed: remove injected slow dependency delay\n"
    )
    return json.dumps(
        {
            "type": "simple_replace",
            "reason": root_cause_summary,
            "edits": [
                {"path": "app.py", "find": find_old, "replace": replace_new},
                {"path": "app.py", "find": find_slow, "replace": replace_slow},
            ],
        },
        indent=2,
    )
