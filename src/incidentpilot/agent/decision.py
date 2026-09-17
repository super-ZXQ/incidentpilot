"""Model-owned investigation decisions with deterministic CI policy.

The graph owns routing and safety. This module owns planning, tool choice,
hypothesis proposal, verification judgement, and root-cause wording.
"""

from __future__ import annotations

import json
from typing import Any

from incidentpilot.llm.provider import FakeLLMProvider, LLMMessage, LLMProvider
from incidentpilot.llm.schemas import (
    HypothesisProposal,
    InvestigationPlan,
    NextAction,
    PatchProposal,
    RootCauseConclusion,
    ToolSelection,
    VerificationDecision,
)

READONLY_TOOLS = (
    "read_metrics",
    "read_logs",
    "inspect_git_history",
    "inspect_git_diff",
    "read_source_code",
    "query_database_readonly",
)

TOOL_ARGUMENT_GUIDE = """
Tool argument contracts (use only these keys and types):
- read_metrics: {"service": string, "window": string}
- read_logs: {"service": string, "window": string}
- inspect_git_history: {"repository": string, "limit": integer}
- inspect_git_diff: {"repository": string, "commit_sha": string}
- read_source_code: {"repository": string, "path": string}
- query_database_readonly: {"query": string containing one read-only SELECT}
Do not add environment, metrics, metric_names, time_range, or window_minutes fields.
Do not guess nested source paths. For the reference/orders_api repository, inspect app.py first.
""".strip()


def _context(incident: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    safe_evidence = [
        {
            "evidence_id": e.get("evidence_id"),
            "source": e.get("source"),
            "source_type": e.get("source_type"),
            "result": e.get("result"),
        }
        for e in evidence
    ]
    return json.dumps({"incident": incident, "evidence": safe_evidence}, default=str)


class AgentDecisionModel:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    @property
    def deterministic(self) -> bool:
        return isinstance(self.provider, FakeLLMProvider)

    async def plan(self, incident: dict[str, Any]) -> InvestigationPlan:
        if self.deterministic:
            return InvestigationPlan(
                focus="Correlate service signals, recent changes, source, and database behavior",
                steps=[
                    "Select the highest-value readonly observation",
                    "Propose an evidence-bound hypothesis",
                    "Collect independent verification evidence",
                    "Conclude only after verification",
                ],
            )
        return await self.provider.complete_structured(
            InvestigationPlan,
            [LLMMessage("user", json.dumps(incident, default=str))],
            system="Plan an incident investigation. Do not claim a root cause yet.",
        )  # type: ignore[return-value]

    async def next_action(
        self, incident: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> NextAction:
        if self.deterministic:
            sources = {str(e.get("source")) for e in evidence}
            if incident.get("rejected_hypotheses"):
                for name, arguments in (
                    (
                        "inspect_git_diff",
                        {"repository": incident.get("repository", ""), "commit_sha": "HEAD"},
                    ),
                    ("query_database_readonly", {"query": "SELECT 1 AS health"}),
                ):
                    if name not in sources:
                        return NextAction(
                            action="collect_evidence",
                            tool_selection=ToolSelection(
                                tool_name=name,
                                reason="collect a new falsification signal after hypothesis rejection",
                                arguments=arguments,
                            ),
                        )
                return NextAction(action="form_hypothesis", reason="new evidence collected after rejection")
            candidates = [
                ("read_metrics", {"service": incident["service"], "window": "incident"}),
                ("read_logs", {"service": incident["service"], "window": "incident"}),
                (
                    "inspect_git_history",
                    {"repository": incident.get("repository", ""), "limit": 5},
                ),
            ]
            for name, arguments in candidates:
                if name not in sources:
                    return NextAction(
                        action="collect_evidence",
                        tool_selection=ToolSelection(
                            tool_name=name,
                            reason=f"{name} provides a distinct signal not yet observed",
                            arguments=arguments,
                        ),
                    )
            return NextAction(action="form_hypothesis", reason="three independent sources collected")
        return await self.provider.complete_structured(
            NextAction,
            [LLMMessage("user", _context(incident, evidence))],
            system=(
                "Choose the single next action. Tool names are restricted to: "
                + ", ".join(READONLY_TOOLS)
                + ". Never invent evidence IDs. Prefer a new evidence source over repeating a "
                "successful source. After at least two useful independent observations, form a "
                "falsifiable hypothesis.\n"
                + TOOL_ARGUMENT_GUIDE
            ),
        )  # type: ignore[return-value]

    async def propose_hypothesis(
        self, incident: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> HypothesisProposal:
        if self.deterministic:
            text = json.dumps([e.get("result") for e in evidence]).lower()
            category = "database/query regression"
            for marker, label in (
                ("null", "null handling regression"),
                ("schema", "schema compatibility regression"),
                ("timeout", "dependency timeout configuration"),
                ("n_plus_one", "N+1 query regression"),
                ("missing_index", "missing database index"),
            ):
                if marker in text:
                    category = label
                    break
            return HypothesisProposal(
                statement=f"{incident['service']} degradation is caused by {category}",
                evidence_ids=[str(e["evidence_id"]) for e in evidence],
                confidence="MEDIUM",
            )
        return await self.provider.complete_structured(
            HypothesisProposal,
            [LLMMessage("user", _context(incident, evidence))],
            system="Propose one falsifiable hypothesis and cite only supplied evidence IDs.",
        )  # type: ignore[return-value]

    async def verification_action(
        self,
        incident: dict[str, Any],
        evidence: list[dict[str, Any]],
        hypothesis: HypothesisProposal,
    ) -> ToolSelection:
        if self.deterministic:
            return ToolSelection(
                tool_name="read_source_code",
                reason="inspect implementation independently of telemetry",
                arguments={"repository": incident.get("repository", ""), "path": "app.py"},
            )
        action = await self.provider.complete_structured(
            NextAction,
            [
                LLMMessage(
                    "user",
                    _context(incident, evidence)
                    + "\nhypothesis="
                    + hypothesis.model_dump_json(),
                )
            ],
            system=(
                "Select one readonly tool that could confirm or reject the hypothesis. "
                "Prefer a source independent from the evidence already cited. Allowed tools: "
                + ", ".join(READONLY_TOOLS)
                + ".\n"
                + TOOL_ARGUMENT_GUIDE
            ),
        )
        if action.tool_selection is None:
            raise ValueError("verification requires a tool selection")
        return action.tool_selection

    async def verify(
        self, hypothesis: HypothesisProposal, evidence: list[dict[str, Any]]
    ) -> VerificationDecision:
        known = {str(e.get("evidence_id")) for e in evidence}
        if self.deterministic:
            cited = set(hypothesis.evidence_ids)
            independent_sources = {str(e.get("source_type")) for e in evidence}
            supported = cited.issubset(known) and len(independent_sources) >= 3
            return VerificationDecision(
                hypothesis_supported=supported,
                notes="independent telemetry/change/source evidence agrees"
                if supported
                else "evidence does not independently verify the hypothesis",
            )
        return await self.provider.complete_structured(
            VerificationDecision,
            [
                LLMMessage(
                    "user",
                    json.dumps(
                        {
                            "hypothesis": hypothesis.model_dump(),
                            "evidence": evidence,
                        },
                        default=str,
                    ),
                )
            ],
            system="Decide whether the new evidence confirms or rejects the hypothesis.",
        )  # type: ignore[return-value]

    async def conclude(
        self,
        incident: dict[str, Any],
        hypothesis: HypothesisProposal,
        evidence: list[dict[str, Any]],
    ) -> RootCauseConclusion:
        if self.deterministic:
            return RootCauseConclusion(
                statement=hypothesis.statement,
                fault_category="code_or_database_regression",
                affected_component=incident["service"],
                causal_facts=[hypothesis.statement],
                supporting_evidence_ids=[str(e["evidence_id"]) for e in evidence],
                confidence="MEDIUM",
            )
        return await self.provider.complete_structured(
            RootCauseConclusion,
            [
                LLMMessage(
                    "user",
                    json.dumps(
                        {"incident": incident, "hypothesis": hypothesis.model_dump(), "evidence": evidence},
                        default=str,
                    ),
                )
            ],
            system=(
                "Conclude only from the verified hypothesis and cite supplied evidence IDs. "
                "Use one fault_category from this stable taxonomy: database_performance, "
                "application_bug, dependency, data_contract, configuration, code_regression."
            ),
        )  # type: ignore[return-value]

    async def patch(
        self, context: dict[str, Any]
    ) -> PatchProposal:
        if self.deterministic:
            raise RuntimeError("deterministic patching is a test-only adapter")
        return await self.provider.complete_structured(
            PatchProposal,
            [LLMMessage("user", json.dumps(context, default=str))],
            system=(
                "Produce a minimal unified diff. Do not modify .git, secrets, credentials, "
                "environment files, or paths outside the repository."
            ),
        )  # type: ignore[return-value]
