"""GitHub Integration layer.

Real GitHub API is used only when GITHUB_INTEGRATION_ENABLED=true and token is set.
Default mode is dry-run/mock so CI never creates external PRs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
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

    def create_pull_request(
        self,
        *,
        run_id: str,
        title: str,
        body: str,
        patch_diff: str,
        base_commit_sha: str,
        base_branch: str = "main",
    ) -> PullRequestResult:
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
            logger.info("mock PR created for run %s", run_id)
            return result

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        branch = f"incidentpilot/{run_id}"
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
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
                create_ref = client.post(
                    f"{self.base_url}/repos/{self.repo}/git/refs",
                    json={"ref": f"refs/heads/{branch}", "sha": base_sha},
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
                return result
        except Exception as exc:
            return PullRequestResult(ok=False, mode="real", branch=branch, error=str(exc))


def default_github_integration() -> GitHubIntegration:
    return GitHubIntegration()
