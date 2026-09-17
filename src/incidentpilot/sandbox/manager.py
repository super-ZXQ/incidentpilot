"""Sandbox Manager: isolated patch application and test execution.

Agent never gets an unrestricted shell. Commands come from server-side policy.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incidentpilot.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Server-side allowlisted test commands (not LLM-generated)
ALLOWED_TEST_COMMANDS = {
    "pytest": ["python", "-m", "pytest", "-q", "--tb=short"],
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
}
# The Docker socket cannot access Docker socket from the sandbox because it is
# never mounted; the name is also denied as a patch target.


def validate_patch_path(repo: Path, raw_path: str) -> Path:
    """Resolve a patch target and reject escape or sensitive paths."""
    normalized = raw_path.replace("\\", "/").removeprefix("a/").removeprefix("b/")
    candidate = Path(normalized)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe patch path: {raw_path}")
    lowered = {part.lower() for part in candidate.parts}
    sensitive = any(
        part in FORBIDDEN_PATCH_PARTS
        or part.startswith(".env.")
        or "credential" in part
        or "secret" in part
        for part in lowered
    )
    if sensitive:
        raise ValueError(f"forbidden patch path: {raw_path}")
    resolved = (repo / candidate).resolve()
    if not resolved.is_relative_to(repo.resolve()):
        raise ValueError(f"patch path outside repository: {raw_path}")
    return resolved


def validate_patch_diff(repo: Path, patch_diff: str) -> None:
    """Validate every path before any patch process is started."""
    import json

    try:
        data = json.loads(patch_diff)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and data.get("type") == "simple_replace":
        edits = data.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ValueError("patch must contain at least one edit")
        for item in edits:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("patch edit path is required")
            validate_patch_path(repo, item["path"])
        return

    paths: list[str] = []
    for line in patch_diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            value = line[4:].split("\t", 1)[0]
            if value != "/dev/null":
                paths.append(value)
    if not paths:
        raise ValueError("unrecognized or empty patch format")
    for path in paths:
        validate_patch_path(repo, path)


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
        root = Path(tempfile.mkdtemp(prefix=f"incidentpilot-{run_id[:8]}-"))
        dest = root / "repo"
        if source.is_dir():
            shutil.copytree(source, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db", "var"))
        else:
            dest.mkdir(parents=True)
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
        validate_patch_diff(repo, patch_diff)
        # Prefer git apply if possible; otherwise write a unified-diff-ish file and
        # apply simple replacements for test fixtures.
        patch_path = root / "patch.diff"
        patch_path.write_text(patch_diff, encoding="utf-8")
        try:
            subprocess.run(
                ["git", "apply", "--check", str(patch_path)],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            subprocess.run(
                ["git", "apply", str(patch_path)],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            return repo
        except Exception:
            # Fallback: interpret a simple "file path / find / replace" JSON patch used in tests
            return self._apply_simple_patch(repo, patch_diff)

    def _apply_simple_patch(self, repo: Path, patch_diff: str) -> Path:
        """Apply a deterministic simple patch format used by Agent FakePatcher.

        Format:
        === FILE: relative/path.py ===
        --- OLD
        +++ NEW
        """
        import json

        # Also accept JSON patches
        try:
            data = json.loads(patch_diff)
            if isinstance(data, dict) and data.get("type") == "simple_replace":
                for item in data.get("edits", []):
                    target = validate_patch_path(repo, item["path"])
                    if not target.exists():
                        raise ValueError(f"target missing: {item['path']}")
                    text = target.read_text(encoding="utf-8")
                    if item["find"] not in text:
                        # Idempotent: already applied from a previous attempt
                        if item["replace"] in text:
                            continue
                        raise ValueError(f"find string not present in {item['path']}")
                    target.write_text(text.replace(item["find"], item["replace"], 1), encoding="utf-8")
                return repo
        except json.JSONDecodeError:
            pass

        # Text block format
        blocks = patch_diff.split("=== FILE:")
        for block in blocks[1:]:
            header, _, body = block.partition("===\n")
            rel = header.strip()
            old_marker = "--- OLD\n"
            new_marker = "+++ NEW\n"
            if old_marker not in body or new_marker not in body:
                continue
            old = body.split(old_marker, 1)[1].split(new_marker, 1)[0]
            new = body.split(new_marker, 1)[1]
            target = validate_patch_path(repo, rel)
            text = target.read_text(encoding="utf-8")
            if old not in text:
                if new in text:
                    continue
                raise ValueError(f"old block not found in {rel}")
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return repo

    def run_tests(self, run_id: str, profile: str = "pytest", timeout: int = 120) -> SandboxResult:
        if profile not in ALLOWED_TEST_COMMANDS:
            raise ValueError(f"test profile not allowlisted: {profile}")
        cmd = ALLOWED_TEST_COMMANDS[profile]
        root = self._workspaces.get(run_id)
        if root is None:
            raise RuntimeError("workspace not created")
        repo = root / "repo"

        use_docker = self.settings.sandbox_enabled
        started = time.perf_counter()
        if use_docker:
            if not docker_available():
                raise RuntimeError("Docker sandbox requested but Docker daemon is unavailable")
            return self._run_in_docker(run_id, repo, cmd, timeout, started)
        return self._run_local(run_id, repo, cmd, timeout, started)

    def _run_local(
        self, run_id: str, repo: Path, cmd: list[str], timeout: int, started: float
    ) -> SandboxResult:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONHOME"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        duration = (time.perf_counter() - started) * 1000
        return SandboxResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=duration,
            command=cmd,
            mode="local",
        )

    def _run_in_docker(
        self, run_id: str, repo: Path, cmd: list[str], timeout: int, started: float
    ) -> SandboxResult:
        image = self.settings.sandbox_image
        network = self.settings.sandbox_network or "none"
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            "--pids-limit",
            "128",
            "-v",
            f"{repo}:/workspace:rw",
            "-w",
            "/workspace",
            image,
            *cmd,
        ]
        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 30,
            )
            duration = (time.perf_counter() - started) * 1000
            return SandboxResult(
                ok=proc.returncode == 0,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_ms=duration,
                command=docker_cmd,
                mode="docker",
            )
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_ms=duration,
                command=docker_cmd,
                mode="docker_error",
            )

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
