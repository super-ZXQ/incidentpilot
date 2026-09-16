"""Hypothesis / Root Cause validation helpers."""

from __future__ import annotations

from typing import Any


def evidence_ids_from(evidence: list[dict[str, Any]]) -> list[str]:
    return [e["evidence_id"] for e in evidence if e.get("evidence_id")]


def build_hypothesis(
    statement: str,
    evidence: list[dict[str, Any]],
    *,
    confidence: str = "MEDIUM",
) -> dict[str, Any]:
    return {
        "statement": statement,
        "evidence_ids": evidence_ids_from(evidence),
        "status": "OPEN",
        "confidence": confidence,
        "verified": False,
        "verification_notes": "",
    }


def verify_hypothesis(
    hypothesis: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    min_evidence: int = 2,
) -> tuple[bool, str]:
    """A hypothesis is only verifiable when it references existing Evidence IDs."""
    known = {e["evidence_id"] for e in evidence}
    refs = set(hypothesis.get("evidence_ids") or [])
    if not refs:
        return False, "hypothesis has no evidence_ids"
    if not refs.issubset(known):
        missing = sorted(refs - known)
        return False, f"hypothesis references unknown evidence: {missing}"
    if len(refs) < min_evidence:
        return False, f"insufficient evidence references ({len(refs)} < {min_evidence})"
    return True, "evidence bindings valid"


def form_root_cause(
    hypothesis: dict[str, Any],
    *,
    affected_component: str,
    fault_category: str = "",
    causal_facts: list[str] | None = None,
) -> dict[str, Any]:
    if not hypothesis.get("evidence_ids"):
        raise ValueError("root cause must reference evidence ids")
    return {
        "summary": hypothesis["statement"],
        "evidence_ids": list(hypothesis["evidence_ids"]),
        "affected_component": affected_component,
        "fault_category": fault_category or "unspecified",
        "causal_facts": causal_facts or [],
    }
