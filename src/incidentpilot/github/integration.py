"""GitHub Integration layer.

Real GitHub API is used only when GITHUB_INTEGRATION_ENABLED=true and token is set.
Default mode is dry-run/mock so CI never creates external PRs.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class PullRequestResult:
    ok: bool
    mode: str  # mock | real
    pr_url: str = ""
    pr_number: int | None = None
    branch: str = ""
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class GitHubIntegration:
    def __init__(
        self,
        *,
        token: str | None = None,
        repo: str | None = None,
        enabled: bool | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        if enabled is None:
            enabled = os.environ.get("GITHUB_INTEGRATION_ENABLED", "false").lower() == "true"
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.repo = repo if repo is not None else os.environ.get("GITHUB_REPO", "")
        self.enabled = enabled and bool(self.token) and bool(self.repo)
        self.base_url = base_url.rstrip("/")
        self.created: list[PullRequestResult] = []
        self.creation_call_count = 0
        self._results_by_key: dict[str, PullRequestResult] = {}

    def create_pull_request(
        self,
        *,
        run_id: str,
        title: str,
        body: str,
        patch_diff: str,
        base_commit_sha: str,
        base_branch: str = "main",
        idempotency_key: str = "",
    ) -> PullRequestResult:
        if idempotency_key and idempotency_key in self._results_by_key:
            return self._results_by_key[idempotency_key]
        if not self.enabled:
            result = PullRequestResult(
                ok=True,
                mode="mock",
                pr_url=f"https://example.invalid/mock-repo/pull/{len(self.created) + 1}",
                pr_number=len(self.created) + 1,
                branch=f"incidentpilot/{run_id}",
                details={
                    "title": title,
                    "body": body[:500],
                    "patch_hash_len": len(patch_diff),
                    "base_commit_sha": base_commit_sha,
                    "note": "dry-run; no external GitHub mutation",
                },
            )
            self.created.append(result)
            self.creation_call_count += 1
            if idempotency_key:
                self._results_by_key[idempotency_key] = result
            logger.info("mock PR created for run %s", run_id)
            return result

        if base_commit_sha == "UNVERSIONED":
            return PullRequestResult(
                ok=False,
                mode="real",
                error="approved artifact has no immutable git base commit",
            )

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        branch = f"incidentpilot/{run_id}"
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                owner = self.repo.split("/", 1)[0]
                existing = client.get(
                    f"{self.base_url}/repos/{self.repo}/pulls",
                    params={"state": "all", "head": f"{owner}:{branch}"},
                )
                if existing.status_code == 200 and existing.json():
                    data = existing.json()[0]
                    result = PullRequestResult(
                        ok=True,
                        mode="real",
                        pr_url=data.get("html_url", ""),
                        pr_number=data.get("number"),
                        branch=branch,
                        details={"deduplicated": True},
                    )
                    if idempotency_key:
                        self._results_by_key[idempotency_key] = result
                    return result
                # Create branch from base commit
                ref_resp = client.get(
                    f"{self.base_url}/repos/{self.repo}/git/ref/heads/{base_branch}"
                )
                if ref_resp.status_code != 200:
                    return PullRequestResult(
                        ok=False,
                        mode="real",
                        error=f"failed to read base ref: {ref_resp.status_code} {ref_resp.text[:200]}",
                    )
                base_sha = ref_resp.json()["object"]["sha"]
                if base_sha != base_commit_sha:
                    return PullRequestResult(
                        ok=False,
                        mode="real",
                        error=(
                            "approved base commit is stale; revalidation required "
                            f"(approved={base_commit_sha}, current={base_sha})"
                        ),
                    )

                commit_resp = client.get(
                    f"{self.base_url}/repos/{self.repo}/git/commits/{base_commit_sha}"
                )
                if commit_resp.status_code != 200:
                    return PullRequestResult(
                        ok=False, mode="real", error="failed to read approved base commit"
                    )
                base_tree_sha = commit_resp.json()["tree"]["sha"]
                changed_paths = []
                for line in patch_diff.splitlines():
                    if line.startswith("+++ b/"):
                        changed_paths.append(line[6:].split("\t", 1)[0])
                    elif line.startswith("+++ ") and line[4:] != "/dev/null":
                        changed_paths.append(line[4:].split("\t", 1)[0])
                changed_paths = list(dict.fromkeys(changed_paths))
                if not changed_paths:
                    return PullRequestResult(
                        ok=False, mode="real", error="approved patch is not a unified diff"
                    )

                with tempfile.TemporaryDirectory(prefix="incidentpilot-github-") as tmp:
                    workspace = Path(tmp)
                    from incidentpilot.sandbox.manager import validate_patch_path

                    for relative in changed_paths:
                        target = validate_patch_path(workspace, relative)
                        contents = client.get(
                            f"{self.base_url}/repos/{self.repo}/contents/{relative}",
                            params={"ref": base_commit_sha},
                        )
                        if contents.status_code != 200:
                            return PullRequestResult(
                                ok=False,
                                mode="real",
                                error=f"cannot load approved base file: {relative}",
                            )
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(base64.b64decode(contents.json()["content"]))
                    patch_path = workspace / "approved.patch"
                    patch_path.write_text(patch_diff, encoding="utf-8")
                    applied = subprocess.run(
                        ["git", "apply", "--check", str(patch_path)],
                        cwd=workspace,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if applied.returncode != 0:
                        return PullRequestResult(
                            ok=False,
                            mode="real",
                            error=f"approved patch no longer applies: {applied.stderr[:300]}",
                        )
                    subprocess.run(
                        ["git", "apply", str(patch_path)],
                        cwd=workspace,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=True,
                    )
                    tree_entries = []
                    for relative in changed_paths:
                        content = (workspace / relative).read_text(encoding="utf-8")
                        blob = client.post(
                            f"{self.base_url}/repos/{self.repo}/git/blobs",
                            json={"content": content, "encoding": "utf-8"},
                        )
                        if blob.status_code not in (200, 201):
                            return PullRequestResult(
                                ok=False, mode="real", error=f"failed to create blob: {relative}"
                            )
                        tree_entries.append(
                            {"path": relative, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}
                        )
                    tree = client.post(
                        f"{self.base_url}/repos/{self.repo}/git/trees",
                        json={"base_tree": base_tree_sha, "tree": tree_entries},
                    )
                    if tree.status_code not in (200, 201):
                        return PullRequestResult(ok=False, mode="real", error="failed to create tree")
                    commit = client.post(
                        f"{self.base_url}/repos/{self.repo}/git/commits",
                        json={
                            "message": title,
                            "tree": tree.json()["sha"],
                            "parents": [base_commit_sha],
                        },
                    )
                    if commit.status_code not in (200, 201):
                        return PullRequestResult(ok=False, mode="real", error="failed to create commit")
                    approved_commit_sha = commit.json()["sha"]
                create_ref = client.post(
                    f"{self.base_url}/repos/{self.repo}/git/refs",
                    json={"ref": f"refs/heads/{branch}", "sha": approved_commit_sha},
                )
                if create_ref.status_code not in (200, 201):
                    return PullRequestResult(
                        ok=False,
                        mode="real",
                        branch=branch,
                        error=f"failed to create branch: {create_ref.status_code} {create_ref.text[:200]}",
                    )

                # Note: applying multi-file patches via Contents API is non-trivial.
                # V1 real mode creates the PR shell; patch application is validated in sandbox.
                pr_resp = client.post(
                    f"{self.base_url}/repos/{self.repo}/pulls",
                    json={
                        "title": title,
                        "head": branch,
                        "base": base_branch,
                        "body": body,
                    },
                )
                if pr_resp.status_code not in (200, 201):
                    return PullRequestResult(
                        ok=False,
                        mode="real",
                        branch=branch,
                        error=f"failed to create PR: {pr_resp.status_code} {pr_resp.text[:200]}",
                    )
                data = pr_resp.json()
                result = PullRequestResult(
                    ok=True,
                    mode="real",
                    pr_url=data.get("html_url", ""),
                    pr_number=data.get("number"),
                    branch=branch,
                    details={"base_sha": base_sha},
                )
                self.created.append(result)
                self.creation_call_count += 1
                if idempotency_key:
                    self._results_by_key[idempotency_key] = result
                return result
        except Exception as exc:
            return PullRequestResult(ok=False, mode="real", branch=branch, error=str(exc))


def default_github_integration() -> GitHubIntegration:
    return GitHubIntegration()
