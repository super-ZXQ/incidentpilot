"""Reproduce offline comparison and three fault-active Docker patch attempts.

No real model or GitHub writes. Ground truth is used only by the scorer.
Run: uv run python scripts/audit_evaluation.py
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from incidentpilot.agent.graph import run_agent_workflow
from incidentpilot.benchmarks.fault_cases import load_fault_cases
from incidentpilot.config import Settings
from incidentpilot.eval.harness import EvaluationHarness
from incidentpilot.models.enums import ApprovalDecision
from incidentpilot.models.schemas import IncidentCreate
from incidentpilot.persistence.jobs import ClaimedRun, create_incident_and_run
from incidentpilot.persistence.session import (
    dispose_engine,
    get_session_factory,
    init_db,
    reset_db_state,
)
from incidentpilot.services.incidents import ApprovalService, RunService
from incidentpilot.tools.data import redact
from incidentpilot.worker import DurableWorker

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "audit" / "artifacts"


def save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(redact(data), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def main():
    os.environ["GITHUB_INTEGRATION_ENABLED"] = "false"
    reset_db_state()
    cases = load_fault_cases(ROOT / "benchmarks" / "fault_cases")
    with tempfile.TemporaryDirectory(prefix="incidentpilot-audit-eval-") as tmp:
        url = "sqlite+aiosqlite:///" + str(Path(tmp) / "eval.db")
        settings = Settings(database_url=url, environment="test", llm_enabled=False, llm_api_key="", tool_backend="fake", sandbox_enabled=True, reference_repo_path=str(ROOT / "reference" / "orders_api"), max_patch_attempts=1)
        await init_db(url)
        try:
            for strategy in ("direct", "fixed", "autonomous"):
                harness = EvaluationHarness(ROOT / "benchmarks" / "fault_cases", settings.model_copy(update={"investigation_only": True}), strategy=strategy)
                report = await harness.run()
                save(f"fake-comparison-{strategy}.json", report.to_dict())
                print(f"{strategy}: {report.completed}/{report.total_cases}, root_cause_accuracy={report.root_cause_accuracy}, P95={report.p95_duration_seconds:.3f}s")
            attempts = []
            for index in (1, 2, 6):
                case = cases[index]  # original slow DB / null / configuration cases
                factory = get_session_factory()
                async with factory() as session:
                    incident, run = await create_incident_and_run(session, incident_data={**IncidentCreate.model_validate(case.incident).model_dump(), "owner_id": "offline-evaluator"}, max_queued_runs=1000)
                execution_settings = settings.model_copy(update={"sandbox_fault_type": case.fault_type})
                state = await run_agent_workflow(incident_pk=incident.id, run_id=run.run_id, settings=execution_settings)
                approval_record = {"status": "NOT_APPLICABLE", "reason": "no passing fault-active artifact", "human_approval": "NOT_RUN"}
                if state["status"] == "WAITING_APPROVAL":
                    async with factory() as session:
                        current = await RunService(session).get(run.run_id)
                        approval, current = await ApprovalService(session).decide(current, ApprovalDecision.APPROVE, actor="offline-test-reviewer", reason="synthetic workflow test only; not a real model diagnosis", owner_id="offline-approval")
                        claim = ClaimedRun(current.incident_pk, current.run_id, current.worker_id, current.attempt_count, True)
                        approval_record = {"decision": approval.decision, "actor": approval.actor, "binding": approval.payload, "human_approval": "NOT_RUN", "mode": "simulated test decision"}
                    await DurableWorker(execution_settings, worker_id=claim.worker_id)._execute_claim(claim)
                async with factory() as session:
                    current = await RunService(session).get(run.run_id)
                    state["persisted_result"] = current.result
                    state["persisted_status"] = current.status
                record = {"fault_case_id": case.id, "fault_type": case.fault_type, "model_mode": "fake", "tool_mode": "synthetic-fake", "test_mode": "real Docker with fault-active oracle", "approval": approval_record, "state": state}
                save(f"offline-patch-{case.fault_type}.json", record)
                attempts.append({"case": case.id, "status": state["status"], "test_status": state.get("result", {}).get("test_status"), "approval": approval_record["human_approval"]})
                print(f"patch {case.fault_type}: {state['status']} / {state.get('result', {}).get('test_status')}")
            save("experiment-status.json", {"timestamp_utc": datetime.now(UTC).isoformat(), "python": platform.python_version(), "platform": platform.platform(), "fake_comparison": "EXECUTED", "real_model_comparison": "NOT_RUN: no API Key", "real_model_remediation": "NOT_RUN: no API Key", "real_github": "NOT_RUN: no authorized dedicated repository", "human_approval": "NOT_RUN: only simulated test decisions", "offline_patch_attempts": attempts})
        finally:
            await dispose_engine()
            reset_db_state()


if __name__ == "__main__":
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(main())
    else:
        asyncio.run(main())
