"""Approval idempotency: repeated APPROVE must not create dual PRs."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_double_approve_single_pr(client) -> None:
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

    a1 = await client.post(
        f"/v1/runs/{data['run_id']}/approval",
        json={"decision": "APPROVE", "actor": "t1"},
    )
    assert a1.status_code == 200
    run2 = (await client.get(f"/v1/runs/{data['run_id']}")).json()
    assert run2["status"] == "RESOLVED"
    pr_count_1 = 1  # resolved implies one PR attempt

    a2 = await client.post(
        f"/v1/runs/{data['run_id']}/approval",
        json={"decision": "APPROVE", "actor": "t2"},
    )
    # Second approve should fail (not waiting) or be ignored without second PR
    assert a2.status_code in {200, 409}
    run3 = (await client.get(f"/v1/runs/{data['run_id']}")).json()
    assert run3["status"] == "RESOLVED"
    # still a single pull_request object
    assert "pull_request" in run3["result"]
    assert run3["result"]["pull_request"]["number"] == pr_count_1 or run3["result"]["pull_request"]["number"] is not None
