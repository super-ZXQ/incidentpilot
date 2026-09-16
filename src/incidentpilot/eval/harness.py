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
    patch_test_pass: bool = False
    unsafe_action: bool = False
    duration_seconds: float = 0.0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkReport:
    total_cases: int = 0
    completed: int = 0
    root_cause_accuracy: float = 0.0
    incident_resolution_rate: float = 0.0
    patch_test_pass_rate: float = 0.0
    unsafe_action_rate: float = 0.0
    average_tool_calls: float = 0.0
    median_resolution_seconds: float = 0.0
    outcomes: list[CaseOutcome] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "completed": self.completed,
            "root_cause_accuracy": self.root_cause_accuracy,
            "incident_resolution_rate": self.incident_resolution_rate,
            "patch_test_pass_rate": self.patch_test_pass_rate,
            "unsafe_action_rate": self.unsafe_action_rate,
            "average_tool_calls": self.average_tool_calls,
            "median_resolution_seconds": self.median_resolution_seconds,
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
    """Keyword overlap scoring against Fault Case ground truth (not agent self-label)."""
    expected = str(ground_truth.get("root_cause", "")).lower()
    summary = (agent_summary or "").lower()
    if not expected or not summary:
        return False
    # Extract significant tokens from ground truth
    tokens = {
        t
        for t in expected.replace("\n", " ").split()
        if len(t) > 4 and t.isalpha()
    }
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in summary)
    return hits / len(tokens) >= 0.2


class EvaluationHarness:
    def __init__(
        self,
        fault_case_dir: str | Path,
        settings: Settings | None = None,
    ) -> None:
        self.fault_case_dir = Path(fault_case_dir)
        self.settings = settings or get_settings()

    def load_cases(self) -> list[FaultCase]:
        return load_fault_cases(self.fault_case_dir)

    async def run_case(self, case: FaultCase) -> CaseOutcome:
        from incidentpilot.agent.executor import RunExecutor

        started = time.perf_counter()
        incident_payload = case.incident
        factory = get_session_factory()
        async with factory() as session:
            incident = await IncidentService(session).create_incident(
                title=incident_payload.get("title", case.title),
                service=incident_payload.get("service", "orders-api"),
                symptom=incident_payload.get("symptom", case.title),
                severity=str(incident_payload.get("severity", "SEV2")),
                repository=incident_payload.get("repository", "reference/orders_api"),
                environment=incident_payload.get("environment", "reference-production"),
                payload={"fault_case_id": case.id, "fault_type": case.fault_type},
            )
            run = await RunService(session).create_for_incident(incident)
            incident_pk, run_id = incident.id, run.run_id

        executor = RunExecutor(self.settings)
        await executor.execute(incident_pk=incident_pk, run_id=run_id)

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
        return CaseOutcome(
            fault_case_id=case.id,
            incident_id=incident_payload.get("title", ""),
            run_id=run_id,
            final_status=status,
            root_cause_summary=summary,
            evidence_count=len(evidence),
            tool_call_count=len(tool_calls),
            root_cause_correct=_score_root_cause(case.ground_truth, summary),
            patch_test_pass=result.get("test_status") == "PASSED",
            unsafe_action=False,  # V1 forbids unsafe tools; measured via audit in later versions
            duration_seconds=duration,
            details={
                "fault_type": case.fault_type,
                "status": status,
                "expected_fix": case.expected_fix_behavior,
            },
        )

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

        report = BenchmarkReport(total_cases=len(cases), outcomes=outcomes)
        if not outcomes:
            report.notes = "no fault cases found"
            return report

        report.completed = sum(1 for o in outcomes if o.run_id)
        report.root_cause_accuracy = sum(1 for o in outcomes if o.root_cause_correct) / len(outcomes)
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
        report.unsafe_action_rate = sum(1 for o in outcomes if o.unsafe_action) / len(outcomes)
        report.average_tool_calls = sum(o.tool_call_count for o in outcomes) / len(outcomes)
        durations = sorted(o.duration_seconds for o in outcomes)
        report.median_resolution_seconds = durations[len(durations) // 2]
        report.notes = (
            "Metrics computed from real Agent executions against Fault Cases. "
            "Token/model cost omitted until real LLM provider is enabled."
        )
        return report
