"""Phase 5 tests: evidence-bound hypothesis and root cause."""

from __future__ import annotations

import asyncio

import pytest

from incidentpilot.agent.reasoning import (
    build_hypothesis,
    form_root_cause,
    verify_hypothesis,
)


def test_hypothesis_requires_evidence_ids() -> None:
    evidence = [
        {"evidence_id": "E1", "content": "metrics"},
        {"evidence_id": "E2", "content": "logs"},
    ]
    hyp = build_hypothesis("db regression", evidence)
    ok, notes = verify_hypothesis(hyp, evidence, min_evidence=2)
    assert ok is True
    assert "valid" in notes

    bad = build_hypothesis("x", [])
    ok2, notes2 = verify_hypothesis(bad, evidence)
    assert ok2 is False
    assert "no evidence" in notes2


def test_root_cause_must_reference_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        form_root_cause({"statement": "x", "evidence_ids": []}, affected_component="svc")


def test_root_cause_from_verified_hypothesis() -> None:
    evidence = [{"evidence_id": "E1"}, {"evidence_id": "E2"}]
    hyp = build_hypothesis("slow query", evidence)
    rc = form_root_cause(hyp, affected_component="orders-api")
    assert rc["evidence_ids"] == ["E1", "E2"]
    assert rc["affected_component"] == "orders-api"


@pytest.mark.asyncio
async def test_agent_run_persists_evidence_bound_root_cause(client) -> None:
    payload = {
        "title": "orders latency p5",
        "service": "orders-api",
        "severity": "SEV2",
        "symptom": "P95 400ms -> 2.8s",
        "repository": "local/orders-api",
        "environment": "reference-production",
    }
    resp = await client.post("/v1/incidents", json=payload)
    data = resp.json()
    for _ in range(80):
        run = (await client.get(f"/v1/runs/{data['run_id']}")).json()
        if run["status"] not in {"PENDING", "RUNNING"}:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("run timeout")

    assert run["root_cause"]["evidence_ids"]
    evidence = (await client.get(f"/v1/runs/{data['run_id']}/evidence")).json()
    known = {e["evidence_id"] for e in evidence}
    assert set(run["root_cause"]["evidence_ids"]).issubset(known)
    # each evidence points to a tool call
    tools = (await client.get(f"/v1/runs/{data['run_id']}/tool-calls")).json()
    tool_ids = {t["tool_call_id"] for t in tools}
    assert all(e["tool_call_id"] in tool_ids for e in evidence)
