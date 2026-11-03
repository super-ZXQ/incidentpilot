"""The containerized investigation needs a repository, not a bare source directory."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from incidentpilot.benchmarks.reference_git_fixture import build_reference_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.STDOUT)


def _source(root: Path) -> Path:
    source = root / "src"
    (source / "tests").mkdir(parents=True)
    (source / "var" / "logs").mkdir(parents=True)
    (source / "__pycache__").mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "faults.py").write_text("HANDLERS = {}\n", encoding="utf-8")
    (source / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
    (source / "var" / "logs" / "orders.jsonl").write_text("{}\n", encoding="utf-8")
    (source / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"stale")
    return source


def test_head_tree_is_the_deployed_source(tmp_path: Path) -> None:
    result = build_reference_repo(_source(tmp_path), tmp_path / "repo")
    repo = tmp_path / "repo"

    messages = _git(repo, "log", "--pretty=%s").splitlines()
    assert messages == ["fault injection and regression tests", "orders-api service"]
    assert _git(repo, "status", "--porcelain") == ""
    assert sorted(_git(repo, "ls-files").split()) == [
        "app.py",
        "faults.py",
        "tests/test_app.py",
    ]
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert result["base_commit_sha"] != result["head_commit_sha"]
    assert len(str(result["head_commit_sha"])) == 40


def test_rebuilding_a_populated_destination_keeps_the_mount_point(tmp_path: Path) -> None:
    destination = tmp_path / "volume"
    source = _source(tmp_path)
    build_reference_repo(source, destination)
    (destination / "untracked").write_text("leftover\n", encoding="utf-8")

    build_reference_repo(source, destination)

    assert destination.is_dir()
    assert not (destination / "untracked").exists()
    assert _git(destination, "status", "--porcelain") == ""


def test_clearing_a_rebuilt_destination_retries_read_only_git_objects(
    tmp_path: Path, monkeypatch
) -> None:
    """Git's loose objects are read-only, and Windows refuses to delete them."""
    import os

    from incidentpilot.benchmarks.reference_git_fixture import _clear

    destination = tmp_path / "volume"
    objects = destination / ".git" / "objects" / "2e"
    objects.mkdir(parents=True)
    loose = objects / "b70d"
    loose.write_text("object", encoding="utf-8")
    os.chmod(loose, 0o444)

    real_unlink = os.unlink
    refusals = {"remaining": 1}

    def windows_like_unlink(path, *args, **kwargs):
        if str(path).endswith("b70d") and refusals["remaining"]:
            refusals["remaining"] -= 1
            raise PermissionError(13, "Access is denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", windows_like_unlink)

    _clear(destination)

    assert refusals["remaining"] == 0, "the first unlink was never refused"
    assert not (destination / ".git").exists()


def test_the_shipped_reference_tree_builds_without_losing_source_files(tmp_path: Path) -> None:
    """The exclusions must not drop anything the reference API actually serves."""
    source = Path(__file__).resolve().parents[1] / "reference" / "orders_api"
    destination = tmp_path / "repo"
    build_reference_repo(source, destination)

    tracked = set(_git(destination, "ls-files").split())
    assert {
        "app.py",
        "faults.py",
        "metrics.py",
        "tests/test_orders_api.py",
        "tests/test_regression.py",
    } <= tracked
    assert (destination / "app.py").read_bytes() == (source / "app.py").read_bytes()
    assert not (destination / "var").exists()
