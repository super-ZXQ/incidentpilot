"""Evaluation Harness: run Fault Cases and compute metrics from real run results.

Ground Truth comes from Fault Cases, never from the Agent.
"""

from __future__ import annotations

import math
import platform
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incidentpilot.benchmarks.fault_cases import FaultCase, load_fault_cases
from incidentpilot.config import Settings, get_settings
from incidentpilot.models.enums import AgentRunStatus
from incidentpilot.persistence.session import get_session_factory
from incidentpilot.services.incidents import IncidentService, RunService


@dataclass
class CaseOutcome:
    fault_case_id: str
    incident_id: str
    run_id: str
    final_status: str
    root_cause_summary: str = ""
    evidence_count: int = 0
    tool_call_count: int = 0
    root_cause_correct: bool = False
    fault_category_correct: bool = False
    affected_component_correct: bool = False
    evidence_supported: bool = False
    tool_selection_score: float = 0.0
    patch_test_pass: bool = False
    unsafe_action: bool = False
    duration_seconds: float = 0.0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkReport:
    strategy: str = "autonomous"
    p95_duration_seconds: float = 0.0
    citation_validity_rate: float = 0.0
    causal_support_rate: float | None = None
    evidence_relevance_rate: float | None = None
    patch_generation_rate: float = 0.0
    patch_application_rate: float = 0.0
    pr_creation_rate: float = 0.0
    environment: dict[str, Any] = field(default_factory=dict)
    tier: str = "workflow_fake"
    total_cases: int = 0
    completed: int = 0
    root_cause_accuracy: float = 0.0
    fault_category_accuracy: float = 0.0
    affected_component_accuracy: float = 0.0
    evidence_support_rate: float = 0.0
    tool_selection_accuracy: float = 0.0
    investigation_success_rate: float = 0.0
    remediation_ready_rate: float = 0.0
    full_resolution_rate: float = 0.0
    incident_resolution_rate: float = 0.0
    patch_test_pass_rate: float = 0.0
    unsafe_action_rate: float = 0.0
    average_tool_calls: float = 0.0
    median_resolution_seconds: float = 0.0
    token_usage: int | None = None
    estimated_cost: float | None = None
    outcomes: list[CaseOutcome] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



def _score_root_cause(ground_truth: dict[str, Any], agent_summary: str) -> bool:
    """Structured scoring preferred; keyword overlap is only a weak fallback signal."""
    from incidentpilot.eval.scoring import score_structured_root_cause

    agent_rc = {
        "summary": agent_summary,
        "evidence_ids": [],
    }
    scores = score_structured_root_cause(ground_truth, agent_rc)
    if scores["structured_ok"]:
        return True
    expected = str(ground_truth.get("root_cause", "")).lower()
    summary = (agent_summary or "").lower()
    if not expected or not summary:
        return False
    tokens = {t for t in expected.replace("\n", " ").split() if len(t) > 4 and t.isalpha()}
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in summary)
    return hits / len(tokens) >= 0.35


class EvaluationHarness:
    def __init__(
        self, fault_case_dir: str | Path, settings: Settings | None = None,
        tier: str = "workflow_fake", strategy: str = "autonomous",
    ) -> None:
        self.fault_case_dir = Path(fault_case_dir)
        self.settings = settings or get_settings()
        if tier not in {"workflow_fake", "real_llm_investigation", "full_remediation"}:
            raise ValueError(f"unknown benchmark tier: {tier}")
        self.tier = tier
        if strategy not in {"direct", "fixed", "autonomous"}:
            raise ValueError("unknown comparison strategy")
        self.strategy = strategy
        self.settings = self.settings.model_copy(update={"evaluation_strategy": strategy})
        if tier == "workflow_fake":
            self.settings = self.settings.model_copy(
                update={"tool_backend": "fake", "llm_enabled": False, "llm_api_key": ""}
            )
        elif tier == "real_llm_investigation":
            self.settings = self.settings.model_copy(update={"investigation_only": True})

    def load_cases(self) -> list[FaultCase]:
        return load_fault_cases(self.fault_case_dir)

    async def run_case(self, case: FaultCase) -> CaseOutcome:
        from incidentpilot.agent.executor import RunExecutor

        started = time.perf_counter()
        incident_payload = case.incident
        if self.tier != "workflow_fake":
            if not self.settings.llm_enabled or not self.settings.llm_api_key:
                raise RuntimeError("real benchmark tier requires LLM_API_KEY and LLM_ENABLED=true")
            if self.settings.tool_backend != "mcp":
                raise RuntimeError("real benchmark tier requires TOOL_BACKEND=mcp")
            await self._setup_and_reproduce(case)
        factory = get_session_factory()
        async with factory() as session:
            incident = await IncidentService(session).create_incident(
                title=incident_payload.get("title", case.title),
                service=incident_payload.get("service", "orders-api"),
                symptom=incident_payload.get("symptom", case.title),
                severity=str(incident_payload.get("severity", "SEV2")),
                repository=incident_payload.get("repository", "reference/orders_api"),
                environment=incident_payload.get("environment", "reference-production"),
                # Ground truth and fault identity must never enter Agent context.
                payload={},
            )
            run = await RunService(session).create_for_incident(incident)
            incident_pk, run_id = incident.id, run.run_id

        execution_settings = self.settings.model_copy(update={"sandbox_fault_type": case.fault_type}) if self.tier == "full_remediation" else self.settings
        executor = RunExecutor(execution_settings)
        try:
            await executor.execute(incident_pk=incident_pk, run_id=run_id)
        finally:
            if self.tier != "workflow_fake":
                await self._reset_fault()

        async with factory() as session:
            run_obj = await RunService(session).get(run_id)
            from sqlalchemy import select

            from incidentpilot.models.db import Evidence, PatchAttempt, ToolCall

            evidence = list(
                (
                    await session.execute(select(Evidence).where(Evidence.run_pk == run_obj.id))
                ).scalars()
            )
            tool_calls = list(
                (
                    await session.execute(select(ToolCall).where(ToolCall.run_pk == run_obj.id))
                ).scalars()
            )
            attempts = list((await session.execute(select(PatchAttempt).where(PatchAttempt.run_pk == run_obj.id))).scalars())
            status = run_obj.status
            root_cause = run_obj.root_cause or {}
            result = run_obj.result or {}

        duration = time.perf_counter() - started
        summary = str(root_cause.get("summary", ""))
        from incidentpilot.eval.scoring import score_structured_root_cause

        evidence_dicts = [{"evidence_id": e.evidence_id, "run_id": run_id, "tool_call_id": e.tool_call_id, "hash": e.content_hash, "result": e.result} for e in evidence]
        calls = [{"tool_call_id": t.tool_call_id, "status": t.status, "output": t.output} for t in tool_calls]
        structured = score_structured_root_cause(case.ground_truth, root_cause, evidence=evidence_dicts, tool_calls=calls, run_id=run_id)
        allowed = {
            "read_metrics", "read_logs", "inspect_git_history", "inspect_git_diff",
            "read_source_code", "query_database_readonly",
        }
        observed_tools = {t.tool_name for t in tool_calls if t.status == "SUCCEEDED"}
        expected_tools = set(case.expected_evidence_sources)
        tool_score = len(observed_tools & expected_tools) / len(expected_tools) if expected_tools else 1.0
        unsafe = any(t.tool_name not in allowed or t.status == "DENIED" for t in tool_calls)
        return CaseOutcome(
            fault_case_id=case.id,
            incident_id=incident_payload.get("title", ""),
            run_id=run_id,
            final_status=status,
            root_cause_summary=summary,
            evidence_count=len(evidence),
            tool_call_count=len(tool_calls),
            root_cause_correct=structured["structured_ok"],
            fault_category_correct=structured["category_ok"],
            affected_component_correct=structured["component_ok"],
            evidence_supported=structured["evidence_bound"],
            tool_selection_score=tool_score,
            patch_test_pass=result.get("test_status") == "PASSED",
            unsafe_action=unsafe,
            duration_seconds=duration,
            details={
                "fault_type": case.fault_type,
                "status": status,
                "expected_fix": case.expected_fix_behavior,
                "citation_valid": structured["evidence_bound"],
                "causal_facts_match": structured["causal_facts_match"],
                "test_scope": result.get("test_scope"),
                "test_executed": result.get("test_status") in {"PASSED", "FAILED"},
                "token_usage": result.get("token_usage"),
                "model": result.get("model"),
                "patch_generated": any(p.patch_diff for p in attempts),
                "patch_applied": any(p.status in {"applied", "TESTS_PASSED", "TESTS_FAILED"} for p in attempts),
                "pull_request": result.get("pull_request"),
            },
        )

    async def _setup_and_reproduce(self, case: FaultCase) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            setup = await client.post(
                f"{self.settings.reference_orders_api_url}/admin/fault",
                json={"fault_type": case.fault_type, "params": case.params},
            )
            setup.raise_for_status()
            reproduced = await client.get(f"{self.settings.reference_orders_api_url}/orders")
            if reproduced.status_code not in {200, 500, 504}:
                raise RuntimeError(f"fault reproduction failed with {reproduced.status_code}")

    async def _reset_fault(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.settings.reference_orders_api_url}/admin/fault/reset"
            )
            response.raise_for_status()

    async def run(self) -> BenchmarkReport:
        cases = self.load_cases()
        outcomes: list[CaseOutcome] = []
        for case in cases:
            try:
                outcome = await self.run_case(case)
            except Exception as exc:
                outcome = CaseOutcome(
                    fault_case_id=case.id,
                    incident_id="",
                    run_id="",
                    final_status=AgentRunStatus.FAILED.value,
                    error=str(exc),
                )
            outcomes.append(outcome)

        report = BenchmarkReport(tier=self.tier, strategy=self.strategy, total_cases=len(cases), outcomes=outcomes, environment={"timestamp_utc": datetime.now(UTC).isoformat(), "python": platform.python_version(), "platform": platform.platform(), "model": self.settings.llm_model if self.settings.llm_enabled else "fake-model-v1", "tool_backend": self.settings.tool_backend, "runs_per_case": 1, "temperature": 0.0})
        if not outcomes:
            report.notes = "no fault cases found"
            return report

        report.completed = sum(1 for o in outcomes if o.run_id)
        report.root_cause_accuracy = sum(1 for o in outcomes if o.root_cause_correct) / len(outcomes)
        report.fault_category_accuracy = sum(1 for o in outcomes if o.fault_category_correct) / len(outcomes)
        report.affected_component_accuracy = sum(1 for o in outcomes if o.affected_component_correct) / len(outcomes)
        report.evidence_support_rate = sum(1 for o in outcomes if o.evidence_supported) / len(outcomes)
        report.tool_selection_accuracy = sum(o.tool_selection_score for o in outcomes) / len(outcomes)
        report.investigation_success_rate = sum(1 for o in outcomes if o.root_cause_correct) / len(outcomes)
        report.incident_resolution_rate = sum(
            1 for o in outcomes if o.final_status == AgentRunStatus.RESOLVED.value
        ) / len(outcomes)
        tested = [o for o in outcomes if o.details.get("test_executed")]
        report.patch_test_pass_rate = (
            sum(1 for o in tested if o.patch_test_pass) / len(tested) if tested else 0.0
        )
        report.remediation_ready_rate = sum(
            1 for o in outcomes if o.final_status in {
                AgentRunStatus.WAITING_APPROVAL.value, AgentRunStatus.RESOLVED.value
            } and o.patch_test_pass and o.details.get("test_scope") == "fault-active"
        ) / len(outcomes)
        report.full_resolution_rate = 0.0  # Requires separate fault-active post-fix reproduction.
        report.unsafe_action_rate = sum(1 for o in outcomes if o.unsafe_action) / len(outcomes)
        report.average_tool_calls = sum(o.tool_call_count for o in outcomes) / len(outcomes)
        durations = sorted(o.duration_seconds for o in outcomes)
        report.median_resolution_seconds = statistics.median(durations)
        report.p95_duration_seconds = durations[max(0, math.ceil(.95 * len(durations)) - 1)]
        report.citation_validity_rate = sum(bool(o.details.get("citation_valid")) for o in outcomes) / len(outcomes)
        report.patch_generation_rate = sum(bool(o.details.get("patch_generated")) for o in outcomes) / len(outcomes)
        report.patch_application_rate = sum(bool(o.details.get("patch_applied")) for o in outcomes) / len(outcomes)
        report.pr_creation_rate = sum(bool((o.details.get("pull_request") or {}).get("mode") == "real") for o in outcomes) / len(outcomes)
        usages = [o.details.get("token_usage") for o in outcomes]
        if all(isinstance(u, dict) and isinstance(u.get("total_tokens"), int) for u in usages):
            report.token_usage = sum(u["total_tokens"] for u in usages)
        report.notes = (
            f"Tier={self.tier}. Ground truth was withheld from Agent context. "
            "Token/model cost is reported only when provider usage metadata is available."
        )
        return report
