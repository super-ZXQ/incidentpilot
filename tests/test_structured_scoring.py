"""Structured root-cause scoring against Fault Case ground truth (no keyword-only)."""

from __future__ import annotations

from incidentpilot.eval.scoring import score_structured_root_cause


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
    assert score_structured_root_cause(gt, good)["structured_ok"] is True
    assert score_structured_root_cause(gt, bad)["structured_ok"] is False
