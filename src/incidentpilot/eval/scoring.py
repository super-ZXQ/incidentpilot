"""Structured root-cause scoring against Fault Case ground truth."""

from __future__ import annotations

from typing import Any

CATEGORY_ALIASES = {
    "database_performance": {
        "n_plus_one_query",
        "slow_database_query",
        "missing_database_index",
        "missing_index",
    },
    "application_bug": {"null_exception", "null_handling_regression"},
    "dependency": {"dependency_timeout", "upstream_timeout"},
    "data_contract": {"schema_mismatch", "schema_compatibility"},
    "configuration": {"incorrect_timeout_configuration", "timeout_configuration"},
    "code_regression": {"bad_query_refactor", "query_regression"},
}


def _category_matches(expected: str, observed: str) -> bool:
    if not expected or not observed:
        return False
    if expected == observed or expected in observed:
        return True
    return observed in CATEGORY_ALIASES.get(expected, set())


def score_structured_root_cause(ground_truth: dict[str, Any], agent_rc: dict[str, Any]) -> dict[str, bool]:
    gt_cat = str(
        ground_truth.get("fault_category") or ground_truth.get("expected_fault_category") or ""
    ).lower()
    gt_comp = str(ground_truth.get("affected_component") or "").lower()
    gt_facts = [str(x).lower() for x in ground_truth.get("causal_facts") or []]

    agent_cat = str(agent_rc.get("fault_category") or "").lower()
    agent_comp = str(agent_rc.get("affected_component") or "").lower()
    agent_facts = [str(x).lower() for x in agent_rc.get("causal_facts") or []]
    agent_stmt = str(agent_rc.get("summary") or agent_rc.get("statement") or "").lower()

    category_ok = _category_matches(gt_cat, agent_cat)
    comp_tokens = {t for t in gt_comp.replace("/", " ").replace("-", " ").split() if len(t) > 3}
    comp_ok = bool(comp_tokens) and any(t in agent_comp or t in agent_stmt for t in comp_tokens)

    facts_hits = 0
    for fact in gt_facts:
        tokens = {t for t in fact.split() if len(t) > 4}
        if tokens and any(t in agent_stmt or any(t in f for f in agent_facts) for t in tokens):
            facts_hits += 1
    facts_ok = (not gt_facts) or (facts_hits / len(gt_facts) >= 0.5)

    evidence_ok = bool(agent_rc.get("evidence_ids") or agent_rc.get("supporting_evidence_ids"))
    structured_ok = category_ok and comp_ok and facts_ok and evidence_ok
    return {
        "category_ok": category_ok,
        "component_ok": comp_ok,
        "causal_facts_ok": facts_ok,
        "evidence_bound": evidence_ok,
        "structured_ok": structured_ok,
    }
