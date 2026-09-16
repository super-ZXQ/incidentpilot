"""Create a real temporary Git repository fixture for reference investigation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=repo,
        text=True,
        stderr=subprocess.STDOUT,
    )


def build_fault_git_fixture(dest: Path, *, fault_commit_message: str = "introduce orders listing regression") -> dict[str, str]:
    """Create dest as a real git repo with base commit + faulty commit."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    _git(dest, "init", "-b", "main")
    _git(dest, "config", "user.email", "fixture@incidentpilot.local")
    _git(dest, "config", "user.name", "IncidentPilot Fixture")

    (dest / "app.py").write_text(
        "def list_orders(db):\n"
        "    orders = db.query('orders').all()\n"
        "    result = []\n"
        "    for o in orders:\n"
        "        items = db.query('items').where(order_id=o.id).all()\n"
        "        result.append({**o, 'items': items})\n"
        "    return result\n",
        encoding="utf-8",
    )
    (dest / "README.md").write_text("orders-api fixture\n", encoding="utf-8")
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", "healthy baseline orders listing")
    base_sha = _git(dest, "rev-parse", "HEAD").strip()

    # Faulty commit: reintroduce N+1 style access with explicit per-order query
    (dest / "app.py").write_text(
        "def list_orders(db):\n"
        "    # N+1 regression: items loaded per order\n"
        "    orders = db.query('orders').all()\n"
        "    result = []\n"
        "    for o in orders:\n"
        "        items = db.query('items').where(order_id=o.id).all()  # N+1\n"
        "        result.append({**o, 'items': items})\n"
        "    return result\n",
        encoding="utf-8",
    )
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", fault_commit_message)
    head_sha = _git(dest, "rev-parse", "HEAD").strip()

    return {"base_commit_sha": base_sha, "faulty_commit_sha": head_sha, "path": str(dest)}


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "var" / "git_fixture"
    print(build_fault_git_fixture(out))
