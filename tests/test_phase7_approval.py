"""Phase 7 tests: approval resume and GitHub mock integration."""

from __future__ import annotations

import asyncio

import pytest

from incidentpilot.github.integration import GitHubIntegration


@pytest.mark.asyncio
async def test_github_mock_mode_creates_pr() -> None:
    gh = GitHubIntegration(enabled=False)
    pr = gh.create_pull_request(
        run_id="RUN-test",
        title="t",
        body="b",
        patch_diff="diff",
        base_commit_sha="abc",
    )
    assert pr.ok is True
    assert pr.mode == "mock"
    assert pr.pr_url
    assert len(gh.created) == 1


@pytest.mark.asyncio
async def test_github_mock_idempotency_key_prevents_duplicate_creation() -> None:
    gh = GitHubIntegration(enabled=False)
    arguments = {
        "run_id": "RUN-crash-window",
        "title": "t",
        "body": "b",
        "patch_diff": "diff",
        "base_commit_sha": "abc",
        "idempotency_key": "github_pr:RUN-crash-window:hash",
    }
    first = gh.create_pull_request(**arguments)
    replayed = gh.create_pull_request(**arguments)
    assert first.pr_url == replayed.pr_url
    assert gh.creation_call_count == 1
    assert len(gh.created) == 1


@pytest.mark.asyncio
async def test_approval_approve_creates_mock_pr(client) -> None:
    payload = {
        "title": "orders latency p7",
        "service": "orders-api",
        "severity": "SEV2",
        "symptom": "P95 400ms -> 2.8s",
        "repository": "local/orders-api",
        "environment": "reference-production",
    }
    resp = await client.post("/v1/incidents", json=payload)
    data = resp.json()
    for _ in range(120):
        run = (await client.get(f"/v1/runs/{data['run_id']}")).json()
        if run["status"] not in {"PENDING", "RUNNING"}:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("run timeout")

    assert run["status"] == "WAITING_APPROVAL"

    # Reject path first on a different mental model: approve this one
    approval = await client.post(
        f"/v1/runs/{data['run_id']}/approval",
        json={"decision": "APPROVE", "actor": "tester", "reason": "looks good"},
    )
    assert approval.status_code == 200
    assert approval.json()["decision"] == "APPROVE"

    for _ in range(50):
        run2 = (await client.get(f"/v1/runs/{data['run_id']}")).json()
        if run2["status"] not in {"RUNNING", "WAITING_APPROVAL"}:
            break
        await asyncio.sleep(0.05)

    assert run2["status"] == "RESOLVED"
    assert run2["result"]["pull_request"]["mode"] == "mock"
    assert run2["result"]["pull_request"]["url"]


@pytest.mark.asyncio
async def test_approval_reject_needs_human(client) -> None:
    payload = {
        "title": "orders latency p7b",
        "service": "orders-api",
        "severity": "SEV2",
        "symptom": "P95 400ms -> 2.8s",
        "repository": "local/orders-api",
        "environment": "reference-production",
    }
    resp = await client.post("/v1/incidents", json=payload)
    data = resp.json()
    for _ in range(120):
        run = (await client.get(f"/v1/runs/{data['run_id']}")).json()
        if run["status"] not in {"PENDING", "RUNNING"}:
            break
        await asyncio.sleep(0.1)
    assert run["status"] == "WAITING_APPROVAL"

    resp2 = await client.post(
        f"/v1/runs/{data['run_id']}/approval",
        json={"decision": "REJECT", "reason": "not confident"},
    )
    assert resp2.status_code == 200
    run2 = (await client.get(f"/v1/runs/{data['run_id']}")).json()
    assert run2["status"] == "NEEDS_HUMAN_INTERVENTION"
