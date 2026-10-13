"""Structured root-cause scoring against Fault Case ground truth (no keyword-only)."""

from __future__ import annotations

from incidentpilot.agent.evidence import content_hash
from incidentpilot.eval.scoring import score_structured_root_cause


def score(ground_truth, root_cause):
    ids = root_cause.get("evidence_ids", [])
    evidence = [{"evidence_id": eid, "run_id": "run", "tool_call_id": eid, "result": {"observation": eid}, "hash": content_hash({"observation": eid})} for eid in ids]
    calls = [{"tool_call_id": eid, "status": "SUCCEEDED", "output": {"observation": eid}} for eid in ids]
    return score_structured_root_cause(ground_truth, root_cause, evidence=evidence, tool_calls=calls, run_id="run")


def test_structured_scoring_prefers_category_and_component() -> None:
    gt = {
        "fault_category": "database_performance",
        "affected_component": "orders-api /orders handler",
        "causal_facts": ["per-order item query", "elevated listing latency"],
    }
    good = {
        "summary": "N+1 per-order item query causes elevated listing latency in orders-api /orders",
        "fault_category": "database_performance",
        "affected_component": "orders-api /orders handler",
        "causal_facts": ["per-order item query", "elevated listing latency"],
        "evidence_ids": ["E1", "E2"],
    }
    bad = {
        "summary": "frontend css regression",
        "fault_category": "ui",
        "affected_component": "static/css",
        "causal_facts": [],
        "evidence_ids": [],
    }
    assert score(gt, good)["structured_ok"] is True
    assert score(gt, bad)["structured_ok"] is False


def test_structured_scoring_accepts_known_subtype_without_keyword_fallback() -> None:
    gt = {
        "fault_category": "database_performance",
        "affected_component": "orders-api /orders handler",
        "causal_facts": ["per-order item query"],
    }
    observed = {
        "summary": "The listing handler performs one item query per order.",
        "fault_category": "n_plus_one_query",
        "affected_component": "orders-api /orders listing path",
        "causal_facts": ["per-order item query"],
        "evidence_ids": ["E1"],
    }
    scores = score(gt, observed)
    assert scores["category_ok"] is True
    assert scores["structured_ok"] is True
