"""Build a real Git repository from the reference source tree.

The containerized worker receives the reference app as a bind mount, so there is no
`.git`: `inspect_git_history` fails, the third required evidence source never arrives,
and runs end in NEEDS_HUMAN_INTERVENTION. This makes a repository whose HEAD tree is
exactly the deployed source, so the git tools, `base_commit_sha` and the sandbox
workspace copy all describe the code the reference API is actually running.

Run as the `incidentpilot-refgit` Compose one-shot, as the same uid the worker runs as:

    python -m incidentpilot.benchmarks.reference_git_fixture \
        --source /reference-src/orders_api --dest /reference-git/orders_api

`git` refuses to read a repository owned by another uid, so the destination volume has
to belong to the worker's uid before this writes into it — the image creates
`/reference-git/orders_api` owned by it for that reason.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# Mirrors the sandbox workspace exclusions so the repository and the copy the tests run
# against contain the same files.
EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", "var", ".git", "node_modules"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".db", ".log", ".sqlite3")

# Held back for the second commit. Splitting the real tree across commits keeps every
# commit a genuine snapshot; nothing here invents content.
DEFERRED_TOP_LEVEL = {"faults.py"}
DEFERRED_DIRS = {"tests"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=repo,
        text=True,
        stderr=subprocess.STDOUT,
    )


def _included(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDED_DIRS for part in relative.parts[:-1]):
        return False
    return not path.name.endswith(EXCLUDED_SUFFIXES)


def _is_deferred(relative_path: str) -> bool:
    parts = relative_path.split("/")
    return parts[0] in DEFERRED_DIRS or (len(parts) == 1 and parts[0] in DEFERRED_TOP_LEVEL)


def _clear(destination: Path) -> None:
    # Git marks loose objects read-only; POSIX unlink ignores that bit, Windows refuses
    # to delete the file. Clear the bit and retry whatever operation failed.
    def make_writable_and_retry(func, path, exc):
        del exc
        os.chmod(path, stat.S_IWRITE)
        func(path)

    # The destination is a mount point, so only its children may be removed.
    for child in destination.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, onexc=make_writable_and_retry)
        else:
            child.unlink()


def _copy(source: Path, destination: Path) -> list[str]:
    copied: list[str] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file() or not _included(path, source):
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(path.relative_to(source).as_posix())
    return copied


def build_reference_repo(source: Path, destination: Path) -> dict[str, object]:
    """Rebuild *destination* as a Git repository whose HEAD tree equals *source*."""
    if not source.is_dir():
        raise SystemExit(f"reference source directory not found: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    # Checked before anything is deleted: rebuilding a repository that another uid owns
    # would fail in `git` anyway, and the check must not destroy a working one first.
    if hasattr(os, "geteuid") and destination.stat().st_uid != os.geteuid():
        raise SystemExit(
            f"{destination} belongs to uid {destination.stat().st_uid}, not {os.geteuid()}: "
            "git would refuse to read the repository built there"
        )
    _clear(destination)
    copied = _copy(source, destination)
    if not copied:
        raise SystemExit(f"no reference files found under {source}")

    _git(destination, "init", "-q", "-b", "main")
    _git(destination, "config", "user.email", "reference@incidentpilot.local")
    _git(destination, "config", "user.name", "IncidentPilot Reference")
    _git(destination, "config", "commit.gpgsign", "false")

    def commit(files: list[str], message: str) -> str | None:
        if not files:
            return None
        _git(destination, "add", "--", *files)
        _git(destination, "commit", "-q", "-m", message)
        return _git(destination, "rev-parse", "HEAD").strip()

    base_sha = commit([name for name in copied if not _is_deferred(name)], "orders-api service")
    head_sha = (
        commit([name for name in copied if _is_deferred(name)], "fault injection and regression tests")
        or base_sha
    )
    if _git(destination, "status", "--porcelain").strip():
        raise SystemExit("the built repository does not match the reference source")
    return {
        "path": str(destination),
        "base_commit_sha": base_sha,
        "head_commit_sha": head_sha or base_sha,
        "files": len(copied),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args(argv)
    print(build_reference_repo(args.source, args.dest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
