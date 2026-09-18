"""Approval idempotency: repeated APPROVE must not create dual PRs."""

from __future__ import annotations

import asyncio

import pytest

from incidentpilot.github.integration import GitHubIntegration


@pytest.mark.asyncio
async def test_double_approve_single_pr(client, monkeypatch) -> None:
    github = GitHubIntegration(enabled=False)
    monkeypatch.setattr(
        "incidentpilot.agent.approval_resume.default_github_integration", lambda: github
    )
    payload = {
        "title": "orders latency idempotency",
        "service": "orders-api",
        "severity": "SEV2",
        "symptom": "P95 400ms -> 2.8s",
        "repository": "local/orders-api",
        "environment": "reference-production",
    }
    resp = await client.post("/v1/incidents", json=payload)
    data = resp.json()
    for _ in range(150):
        run = (await client.get(f"/v1/runs/{data['run_id']}")).json()
        if run["status"] not in {"PENDING", "RUNNING"}:
            break
        await asyncio.sleep(0.1)
    assert run["status"] == "WAITING_APPROVAL"

    a1, a2 = await asyncio.gather(
        client.post(
            f"/v1/runs/{data['run_id']}/approval",
            json={"decision": "APPROVE", "actor": "t1"},
        ),
        client.post(
            f"/v1/runs/{data['run_id']}/approval",
            json={"decision": "APPROVE", "actor": "t2"},
        ),
    )
    assert sorted([a1.status_code, a2.status_code]) == [200, 409]
    run2 = (await client.get(f"/v1/runs/{data['run_id']}")).json()
    assert run2["status"] == "RESOLVED"
    run3 = (await client.get(f"/v1/runs/{data['run_id']}")).json()
    assert run3["status"] == "RESOLVED"
    assert "pull_request" in run3["result"]
    assert github.creation_call_count == 1
    assert len(github.created) == 1
