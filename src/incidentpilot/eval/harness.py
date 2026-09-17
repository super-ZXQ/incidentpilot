"""Evaluation Harness: run Fault Cases and compute metrics from real run results.

Ground Truth comes from Fault Cases, never from the Agent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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
        return {
            "tier": self.tier,
            "total_cases": self.total_cases,
            "completed": self.completed,
            "root_cause_accuracy": self.root_cause_accuracy,
            "fault_category_accuracy": self.fault_category_accuracy,
            "affected_component_accuracy": self.affected_component_accuracy,
            "evidence_support_rate": self.evidence_support_rate,
            "tool_selection_accuracy": self.tool_selection_accuracy,
            "investigation_success_rate": self.investigation_success_rate,
            "remediation_ready_rate": self.remediation_ready_rate,
            "full_resolution_rate": self.full_resolution_rate,
            "incident_resolution_rate": self.incident_resolution_rate,
            "patch_test_pass_rate": self.patch_test_pass_rate,
            "unsafe_action_rate": self.unsafe_action_rate,
            "average_tool_calls": self.average_tool_calls,
            "median_resolution_seconds": self.median_resolution_seconds,
            "token_usage": self.token_usage,
            "estimated_cost": self.estimated_cost,
            "notes": self.notes,
            "outcomes": [
                {
                    "fault_case_id": o.fault_case_id,
                    "run_id": o.run_id,
                    "final_status": o.final_status,
                    "root_cause_correct": o.root_cause_correct,
                    "patch_test_pass": o.patch_test_pass,
                    "tool_call_count": o.tool_call_count,
                    "evidence_count": o.evidence_count,
                    "duration_seconds": o.duration_seconds,
                    "error": o.error,
                }
                for o in self.outcomes
            ],
        }


def _score_root_cause(ground_truth: dict[str, Any], agent_summary: str) -> bool:
    """Structured scoring preferred; keyword overlap is only a weak fallback signal."""
    from incidentpilot.eval.scoring import score_structured_root_cause

    agent_rc = {
        "summary": agent_summary,
        "evidence_ids": ["E"],
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
        self,
        fault_case_dir: str | Path,
        settings: Settings | None = None,
        tier: str = "workflow_fake",
    ) -> None:
        self.fault_case_dir = Path(fault_case_dir)
        self.settings = settings or get_settings()
        if tier not in {"workflow_fake", "real_llm_investigation", "full_remediation"}:
            raise ValueError(f"unknown benchmark tier: {tier}")
        self.tier = tier
        if tier == "workflow_fake":
            self.settings = self.settings.model_copy(
                update={"tool_backend": "fake", "llm_enabled": False, "llm_api_key": ""}
            )

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

        executor = RunExecutor(self.settings)
        try:
            await executor.execute(incident_pk=incident_pk, run_id=run_id)
        finally:
            if self.tier != "workflow_fake":
                await self._reset_fault()

        async with factory() as session:
            run_obj = await RunService(session).get(run_id)
            from sqlalchemy import select

            from incidentpilot.models.db import Evidence, ToolCall

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
            status = run_obj.status
            root_cause = run_obj.root_cause or {}
            result = run_obj.result or {}

        duration = time.perf_counter() - started
        summary = str(root_cause.get("summary", ""))
        from incidentpilot.eval.scoring import score_structured_root_cause

        structured = score_structured_root_cause(case.ground_truth, root_cause)
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
            },
        )

    async def _setup_and_reproduce(self, case: FaultCase) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            setup = await client.post(
                f"{self.settings.reference_orders_api_url}/admin/fault",
                json={"fault_type": case.fault_type, "params": case.params, "case_id": case.id},
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

        report = BenchmarkReport(tier=self.tier, total_cases=len(cases), outcomes=outcomes)
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
        tested = [o for o in outcomes if o.final_status in {
            AgentRunStatus.WAITING_APPROVAL.value,
            AgentRunStatus.RESOLVED.value,
        }]
        report.patch_test_pass_rate = (
            sum(1 for o in tested if o.patch_test_pass) / len(tested) if tested else 0.0
        )
        report.remediation_ready_rate = sum(
            1 for o in outcomes if o.final_status in {
                AgentRunStatus.WAITING_APPROVAL.value, AgentRunStatus.RESOLVED.value
            } and o.patch_test_pass
        ) / len(outcomes)
        report.full_resolution_rate = sum(
            1 for o in outcomes if o.final_status == AgentRunStatus.RESOLVED.value
        ) / len(outcomes)
        report.unsafe_action_rate = sum(1 for o in outcomes if o.unsafe_action) / len(outcomes)
        report.average_tool_calls = sum(o.tool_call_count for o in outcomes) / len(outcomes)
        durations = sorted(o.duration_seconds for o in outcomes)
        report.median_resolution_seconds = durations[len(durations) // 2]
        report.notes = (
            f"Tier={self.tier}. Ground truth was withheld from Agent context. "
            "Token/model cost is reported only when provider usage metadata is available."
        )
        return report
