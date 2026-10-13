"""GitHub Integration layer.

Real GitHub API is used only when GITHUB_INTEGRATION_ENABLED=true and token is set.
Default mode is dry-run/mock so CI never creates external PRs.
"""

from __future__ import annotations

import base64
import logging
import os
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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if enabled is None:
            enabled = os.environ.get("GITHUB_INTEGRATION_ENABLED", "false").lower() == "true"
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.repo = repo if repo is not None else os.environ.get("GITHUB_REPO", "")
        self.requested_enabled = enabled
        self.enabled = enabled and bool(self.token) and bool(self.repo)
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self.created: list[PullRequestResult] = []
        self.creation_call_count = 0
        self._results_by_key: dict[str, PullRequestResult] = {}

    def create_pull_request(self, **kwargs) -> PullRequestResult:
        """Synchronous mock compatibility; real workflow uses async HTTP."""
        if self.enabled:
            import asyncio

            return asyncio.run(self.acreate_pull_request(**kwargs))
        key = kwargs.get("idempotency_key", "")
        if key in self._results_by_key:
            return self._results_by_key[key]
        if self.requested_enabled:
            return PullRequestResult(ok=False, mode="real", error="enabled GitHub integration requires token and repo")
        result = PullRequestResult(
            ok=True, mode="mock", pr_url=f"https://example.invalid/mock-repo/pull/{len(self.created) + 1}",
            pr_number=len(self.created) + 1, branch=f"incidentpilot/{kwargs['run_id']}",
            details={"note": "dry-run; no external GitHub mutation", "base_commit_sha": kwargs["base_commit_sha"]},
        )
        self.created.append(result)
        self.creation_call_count += 1
        if key:
            self._results_by_key[key] = result
        return result

    async def acreate_pull_request(
        self, *, run_id: str, title: str, body: str, patch_diff: str,
        base_commit_sha: str, base_branch: str = "main", idempotency_key: str = "",
        authorize=None,
    ) -> PullRequestResult:
        if not self.enabled:
            return self.create_pull_request(run_id=run_id, title=title, body=body, patch_diff=patch_diff,
                                            base_commit_sha=base_commit_sha, idempotency_key=idempotency_key)
        from incidentpilot.sandbox.manager import (
            hash_patch,
            patch_paths,
            validate_patch_path,
            workspace_snapshot,
        )
        from incidentpilot.sandbox.process import run_process

        branch = f"incidentpilot/{run_id}-{hash_patch(patch_diff)[:12]}"
        endpoint = f"{self.base_url}/repos/{self.repo}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            if len(base_commit_sha) != 40 or any(c not in "0123456789abcdef" for c in base_commit_sha):
                raise ValueError("approved artifact has no immutable git base commit")
            async with httpx.AsyncClient(timeout=30.0, headers=headers, transport=self.transport) as client:
                async def get(path, **kwargs):
                    response = await client.get(endpoint + path, **kwargs)
                    response.raise_for_status()
                    return response.json()

                async def post(path, payload):
                    if authorize is not None:
                        await authorize()
                    response = await client.post(endpoint + path, json=payload)
                    response.raise_for_status()
                    return response.json()

                owner = self.repo.split("/", 1)[0]
                params = {"state": "all", "head": f"{owner}:{branch}"}
                existing_prs = await get("/pulls", params=params)
                base = await get(f"/git/ref/heads/{base_branch}")
                if base["object"]["sha"] != base_commit_sha:
                    raise ValueError("approved base commit is stale; revalidation required")
                base_commit = await get(f"/git/commits/{base_commit_sha}")
                base_tree = base_commit["tree"]["sha"]
                base_files = await get(f"/git/trees/{base_tree}", params={"recursive": "1"})
                if base_files.get("truncated"):
                    raise ValueError("base tree is truncated")
                entries = {entry["path"]: entry for entry in base_files["tree"]}
                with tempfile.TemporaryDirectory(prefix="incidentpilot-github-") as tmp:
                    workspace = Path(tmp) / "repo"
                    workspace.mkdir()
                    paths = patch_paths(workspace, patch_diff)
                    if patch_diff.lstrip().startswith("{"):
                        raise ValueError("real GitHub requires a unified diff")
                    for relative in paths:
                        entry = entries.get(relative)
                        if entry is None:
                            continue  # Git apply validates new-file headers.
                        if entry["type"] != "blob" or entry["mode"] != "100644":
                            raise ValueError("unsupported base file type or mode")
                        blob = await get(f"/git/blobs/{entry['sha']}")
                        target = validate_patch_path(workspace, relative)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(base64.b64decode(blob["content"], validate=False))
                    before = workspace_snapshot(workspace)
                    patch_file = Path(tmp) / "approved.patch"
                    patch_file.write_bytes(patch_diff.encode("utf-8"))
                    for args in (["--check"], []):
                        code, _, error = await run_process(["git", "-c", "core.autocrlf=false", "apply", *args, str(patch_file)], cwd=workspace, timeout=30)
                        if code:
                            raise ValueError(f"approved patch does not apply: {error[:300]}")
                    after = workspace_snapshot(workspace)
                    if {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)} != paths:
                        raise ValueError("final change paths differ from approval")
                    changes = []
                    for relative in sorted(paths):
                        target = workspace / relative
                        sha = None
                        if target.exists():
                            blob = await post("/git/blobs", {"content": base64.b64encode(target.read_bytes()).decode("ascii"), "encoding": "base64"})
                            sha = blob["sha"]
                        changes.append({"path": relative, "mode": "100644", "type": "blob", "sha": sha})
                    tree = await post("/git/trees", {"base_tree": base_tree, "tree": changes})

                # A crash after branch creation is reconciled against the exact
                # approved tree and parent. Existing branches are never overwritten.
                ref_response = await client.get(endpoint + f"/git/ref/heads/{branch}")
                if ref_response.status_code == 404:
                    commit = await post("/git/commits", {"message": title, "tree": tree["sha"], "parents": [base_commit_sha]})
                    if authorize is not None:
                        await authorize()
                    created = await client.post(endpoint + "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
                    if created.status_code not in {200, 201, 422}:
                        created.raise_for_status()
                    remote_ref = await get(f"/git/ref/heads/{branch}")
                else:
                    ref_response.raise_for_status()
                    remote_ref = ref_response.json()
                remote_commit = await get(f"/git/commits/{remote_ref['object']['sha']}")
                if (remote_commit["tree"]["sha"] != tree["sha"]
                        or [parent["sha"] for parent in remote_commit["parents"]] != [base_commit_sha]):
                    raise ValueError("remote branch differs from approved artifact")
                if not existing_prs:
                    # Requery handles another recovery completing while we checked.
                    existing_prs = await get("/pulls", params=params)
                if existing_prs:
                    data = existing_prs[0]
                    if data.get("head", {}).get("sha") != remote_ref["object"]["sha"]:
                        raise ValueError("existing PR head differs from approved artifact")
                else:
                    if authorize is not None:
                        await authorize()
                    response = await client.post(endpoint + "/pulls", json={"title": title, "head": branch, "base": base_branch, "body": body})
                    if response.status_code == 422:
                        matches = await get("/pulls", params=params)
                        if not matches or matches[0].get("head", {}).get("sha") != remote_ref["object"]["sha"]:
                            raise ValueError("PR conflict could not be reconciled")
                        data = matches[0]
                    else:
                        response.raise_for_status()
                        data = response.json()
                result = PullRequestResult(ok=True, mode="real", pr_url=data["html_url"], pr_number=data["number"], branch=branch,
                                           details={"base_sha": base_commit_sha, "tree_sha": tree["sha"], "commit_sha": remote_ref["object"]["sha"], "deduplicated": bool(existing_prs)})
                self.created.append(result)
                self.creation_call_count += int(not existing_prs)
                return result
        except Exception as exc:
            retryable = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or (
                isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code == 429 or exc.response.status_code >= 500))
            # Do not include remote response bodies or credentials in persisted errors.
            error = f"GitHub HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else str(exc)
            return PullRequestResult(ok=False, mode="real", branch=branch, error=error,
                                     details={"retryable": retryable})


def default_github_integration() -> GitHubIntegration:
    return GitHubIntegration()
