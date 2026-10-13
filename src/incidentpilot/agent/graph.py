"""LangGraph agent workflow with Tool Gateway and Sandbox integration."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from incidentpilot.agent.decision import READONLY_TOOLS, AgentDecisionModel
from incidentpilot.agent.evidence import content_hash, valid_evidence
from incidentpilot.config import Settings
from incidentpilot.llm.provider import build_llm_provider
from incidentpilot.models.enums import AgentRunStatus, WorkflowState
from incidentpilot.persistence import repo
from incidentpilot.persistence.session import get_session_factory
from incidentpilot.tools.fake import register_fake_readonly_tools
from incidentpilot.tools.gateway import BudgetTracker, ToolGateway, ToolRegistry
from incidentpilot.tools.mcp_adapter import register_mcp_readonly_tools

logger = logging.getLogger(__name__)
_TEST_CHECKPOINTER: Any = None


async def _persist_patch_artifact_once(run_id: str, artifact: Any) -> str:
    """Persist immutable output once when a checkpointed node is replayed."""
    from sqlalchemy import select

    from incidentpilot.models.db import AgentRun, PatchArtifact

    factory = get_session_factory()
    async with factory() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))
        ).scalar_one_or_none()
        if run is None:
            return artifact.patch_artifact_id
        from incidentpilot.persistence.ownership import fence

        await fence(session, run_id)
        existing = (
            await session.execute(
                select(PatchArtifact).where(
                    PatchArtifact.run_pk == run.id,
                    PatchArtifact.patch_hash == artifact.patch_hash,
                    PatchArtifact.test_run_id == artifact.test_run_id,
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing.patch_artifact_id
        session.add(
            PatchArtifact(
                patch_artifact_id=artifact.patch_artifact_id,
                run_pk=run.id,
                base_commit_sha=artifact.base_commit_sha,
                patch_diff=artifact.patch_diff,
                patch_hash=artifact.patch_hash,
                test_run_id=artifact.test_run_id,
                immutable=True,
            )
        )
        await session.commit()
        return artifact.patch_artifact_id


class AgentState(TypedDict, total=False):
    run_id: str
    incident_id: str
    incident_title: str
    service: str
    symptom: str
    environment: str
    repository: str
    workflow_state: str
    plan: list[str]
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    root_cause: dict[str, Any] | None
    tool_calls: list[dict[str, Any]]
    patch_attempts: list[dict[str, Any]]
    patch_artifact: dict[str, Any] | None
    approval_required: bool
    status: str
    error: str | None
    result: dict[str, Any]
    limits: dict[str, Any]
    trace_id: str


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def build_tool_gateway(settings: Settings) -> tuple[ToolGateway, Any]:
    registry = ToolRegistry()
    if settings.tool_backend == "fake":
        adapter = register_fake_readonly_tools(registry)
    else:
        from incidentpilot.tools.mcp_adapter import MCPReadonlyAdapter

        adapter = MCPReadonlyAdapter(live=True, settings=settings)
        register_mcp_readonly_tools(registry, adapter)
    budget = BudgetTracker(
        max_tool_calls=settings.max_tool_calls,
        max_steps=settings.max_investigation_steps,
    )
    gateway = ToolGateway(registry=registry, budget=budget)
    return gateway, adapter


async def run_agent_workflow(
    *,
    incident_pk: str,
    run_id: str,
    settings: Settings,
    recover: bool = False,
) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import select

        from incidentpilot.models.db import (
            AgentRun,
            Evidence,
            Hypothesis,
            Incident,
            PatchAttempt,
            TestRun,
            ToolCall,
        )
        from incidentpilot.sandbox.manager import hash_patch
        from incidentpilot.services.incidents import RunService

        incident = await session.get(Incident, incident_pk)
        run_result = await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))
        run = run_result.scalar_one_or_none()
        if incident is None or run is None:
            raise ValueError("incident or run not found")

        service = RunService(session)
        await service.mark_running(run)

        gateway, _adapter = build_tool_gateway(settings)

        state: AgentState = {
            "run_id": run.run_id,
            "incident_id": incident.incident_id,
            "incident_title": incident.title,
            "service": incident.service,
            "symptom": incident.symptom,
            "environment": incident.environment,
            "repository": incident.repository,
            "workflow_state": WorkflowState.INCIDENT_RECEIVED.value,
            "plan": [],
            "evidence": [],
            "hypotheses": [],
            "root_cause": None,
            "tool_calls": [],
            "patch_attempts": [],
            "patch_artifact": None,
            "approval_required": False,
            "status": AgentRunStatus.RUNNING.value,
            "error": None,
            "result": {},
            "limits": {
                "max_investigation_steps": settings.max_investigation_steps,
                "max_tool_calls": settings.max_tool_calls,
                "max_patch_attempts": settings.max_patch_attempts,
                "run_timeout_seconds": settings.run_timeout_seconds,
            },
            "trace_id": run.trace_id,
        }

        # Release the snapshot opened by mark_running's refresh before
        # checkpoint setup creates concurrent indexes on its own connection.
        await session.commit()
        if hasattr(_adapter, "connect"):
            await _adapter.connect()
        try:
            final_state = await _execute_graph(state, settings, gateway, recover=recover)
        finally:
            if hasattr(_adapter, "disconnect"):
                await _adapter.disconnect()

        status = AgentRunStatus(final_state.get("status", AgentRunStatus.FAILED.value))

        from incidentpilot.persistence.ownership import fence

        await fence(session, run_id)
        # Persist investigation artifacts BEFORE marking terminal status so
        # API consumers never observe a finished run without evidence/tool calls.
        existing_tool_ids = set(
            (await session.execute(select(ToolCall.tool_call_id).where(ToolCall.run_pk == run.id)))
            .scalars()
            .all()
        )
        for item in final_state.get("tool_calls") or []:
            tool_call_id = item.get("tool_call_id") or repo.new_id("TC")
            if tool_call_id in existing_tool_ids:
                continue
            session.add(
                ToolCall(
                    tool_call_id=tool_call_id,
                    run_pk=run.id,
                    tool_name=item.get("tool_name", "unknown"),
                    input=item.get("input") or {},
                    output=item.get("output") or {},
                    status=item.get("status", "SUCCEEDED"),
                    error=item.get("error"),
                    latency_ms=item.get("latency_ms"),
                    attempt_count=item.get("attempt_count", 1),
                    error_category=item.get("error_category"),
                    trace_id=item.get("trace_id", run.trace_id),
                    created_at=datetime.fromisoformat(item["created_at"]) if item.get("created_at") else repo.utcnow(),
                )
            )
        existing_evidence_ids = set(
            (await session.execute(select(Evidence.evidence_id).where(Evidence.run_pk == run.id)))
            .scalars()
            .all()
        )
        for item in final_state.get("evidence") or []:
            evidence_id = item.get("evidence_id") or repo.new_id("EVID")
            if evidence_id in existing_evidence_ids:
                continue
            session.add(
                Evidence(
                    evidence_id=evidence_id,
                    run_pk=run.id,
                    source=item.get("source", ""),
                    source_type=item.get("source_type", "other"),
                    tool_call_id=item.get("tool_call_id"),
                    content=item.get("content", ""),
                    summary=item.get("summary", ""),
                    content_hash=item.get("hash", ""),
                    result=item.get("result") or {},
                    timestamp=datetime.fromisoformat(item["timestamp"]) if item.get("timestamp") else repo.utcnow(),
                )
            )
        for item in final_state.get("hypotheses") or []:
            session.add(
                Hypothesis(
                    run_pk=run.id,
                    statement=item.get("statement", ""),
                    evidence_ids=item.get("evidence_ids") or [],
                    status=item.get("status", "OPEN"),
                    confidence=item.get("confidence", "MEDIUM"),
                    verified=bool(item.get("verified")),
                    verification_notes=item.get("verification_notes", ""),
                )
            )
        existing_test_ids = set((await session.execute(select(TestRun.test_run_id).where(TestRun.run_pk == run.id))).scalars())
        for item in final_state.get("patch_attempts") or []:
            patch_diff = item.get("patch_diff", "")
            session.add(
                PatchAttempt(
                    run_pk=run.id,
                    attempt_number=int(item.get("attempt", 1)),
                    patch_diff=patch_diff,
                    patch_hash=hash_patch(patch_diff) if patch_diff else "",
                    status=item.get("status", "CREATED"),
                    notes=item.get("reflection", ""),
                )
            )
            if item.get("test_run_id") and item["test_run_id"] not in existing_test_ids:
                session.add(
                    TestRun(
                        test_run_id=item["test_run_id"],
                        run_pk=run.id,
                        status="PASSED" if item.get("test_ok") else "FAILED",
                        exit_code=item.get("test_exit_code"),
                        duration_ms=item.get("test_duration_ms"),
                        stdout=item.get("test_stdout", ""),
                        stderr=item.get("test_stderr", ""),
                        command=item.get("test_profile", "pytest_regression"),
                        patch_hash=hash_patch(patch_diff),
                        base_commit_sha=item.get("base_commit_sha", ""),
                        mode=item.get("test_mode", "unknown"),
                    )
                )
        run.plan = list(final_state.get("plan") or [])
        run.tool_call_count = len(final_state.get("tool_calls") or [])
        run.patch_attempt_count = len(final_state.get("patch_attempts") or [])
        await session.commit()

        await service.mark_status(
            run,
            status,
            workflow_state=final_state.get("workflow_state", WorkflowState.FAILED.value),
            error=final_state.get("error"),
            result={**(final_state.get("result") or {}), "hypotheses": final_state.get("hypotheses") or []},
            root_cause=final_state.get("root_cause"),
            finished=status
            in {
                AgentRunStatus.INVESTIGATION_COMPLETE,
                AgentRunStatus.RESOLVED,
                AgentRunStatus.INSUFFICIENT_EVIDENCE,
                AgentRunStatus.NEEDS_HUMAN_INTERVENTION,
                AgentRunStatus.FAILED,
            },
        )
        await repo.record_audit(
            session,
            event_type="agent_run_finished",
            message=f"run {run_id} finished with {status.value}",
            run_pk=run.id,
            data={"workflow_state": final_state.get("workflow_state")},
        )
        return final_state


async def _execute_graph(
    state: AgentState,
    settings: Settings,
    gateway: ToolGateway,
    *,
    recover: bool = False,
) -> AgentState:
    try:
        from incidentpilot.persistence.ownership import workflow_lock

        async with workflow_lock(state["run_id"], settings):
            return await _run_with_langgraph(state, settings, gateway, recover=recover)
    except Exception as exc:
        from incidentpilot.persistence.ownership import LostLeaseError

        if isinstance(exc, LostLeaseError):
            raise
        logger.exception("LangGraph execution failed")
        return {
            **state,
            "status": AgentRunStatus.FAILED.value,
            "workflow_state": WorkflowState.FAILED.value,
            "error": f"graph execution failed: {exc}",
            "result": {"fallback": True},
        }


async def _call_tool(
    gateway: ToolGateway,
    state: AgentState,
    name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    result = await gateway.call(
        name,
        payload,
        run_id=state.get("run_id", ""),
        trace_id=state.get("trace_id", ""),
    )
    return result.to_dict()


async def _run_with_langgraph(
    state: AgentState,
    settings: Settings,
    gateway: ToolGateway,
    *,
    resume_value: str | None = None,
    recover: bool = False,
) -> AgentState:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    provider = build_llm_provider(
        llm_enabled=settings.llm_enabled,
        provider_name=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )
    decision_model = AgentDecisionModel(provider)

    def incident_context(s: AgentState) -> dict[str, Any]:
        return {
            "incident_id": s["incident_id"],
            "title": s["incident_title"],
            "service": s["service"],
            "symptom": s["symptom"],
            "environment": s["environment"],
            "repository": s["repository"],
            "rejected_hypotheses": sum(
                1 for h in (s.get("hypotheses") or []) if h.get("status") == "REJECTED"
            ),
        }

    async def plan_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.PLAN.value
        if settings.evaluation_strategy == "autonomous":
            plan = await decision_model.plan(incident_context(s))
        else:
            from incidentpilot.llm.schemas import InvestigationPlan

            plan = InvestigationPlan(focus="controlled evaluation baseline", steps=["Collect fixed context", "Diagnose from observations"])
        s["plan"] = plan.steps
        s["result"] = {
            **(s.get("result") or {}),
            "model": {"provider": provider.provider_name, "model": provider.model_name},
            "plan_focus": plan.focus,
        }
        return s

    async def collect_evidence_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.COLLECT_EVIDENCE.value
        tool_calls: list[dict[str, Any]] = list(s.get("tool_calls") or [])
        evidence: list[dict[str, Any]] = list(s.get("evidence") or [])

        while gateway.budget.can_step():
            gateway.budget.record_step()
            decision_context = incident_context(s)
            recent_failures = [
                {"tool_name": call.get("tool_name"), "error": call.get("error")}
                for call in tool_calls[-3:]
                if call.get("status") != "SUCCEEDED"
            ]
            if recent_failures:
                decision_context["recent_tool_errors"] = recent_failures
            if settings.evaluation_strategy in {"direct", "fixed"}:
                from incidentpilot.llm.schemas import NextAction, ToolSelection

                fixed = [("read_metrics", {"service": s["service"]}), ("read_logs", {"service": s["service"]}), ("inspect_git_history", {"repository": s["repository"], "limit": 5})]
                if settings.evaluation_strategy == "direct":
                    fixed.append(("read_source_code", {"repository": s["repository"], "path": "app.py"}))
                index = len(tool_calls)
                action = NextAction(action="form_hypothesis") if index >= len(fixed) else NextAction(action="collect_evidence", tool_selection=ToolSelection(tool_name=fixed[index][0], arguments=fixed[index][1], reason="fixed baseline observation"))
            else:
                action = await decision_model.next_action(decision_context, evidence)
            if action.action == "form_hypothesis":
                break
            if action.action in {"insufficient_evidence", "needs_human"}:
                s["workflow_state"] = (
                    WorkflowState.INSUFFICIENT_EVIDENCE.value
                    if action.action == "insufficient_evidence"
                    else WorkflowState.NEEDS_HUMAN_INTERVENTION.value
                )
                s["status"] = (
                    AgentRunStatus.INSUFFICIENT_EVIDENCE.value
                    if action.action == "insufficient_evidence"
                    else AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
                )
                s["result"] = {**(s.get("result") or {}), "stop_reason": action.reason}
                break
            selection = action.tool_selection
            if action.action != "collect_evidence" or selection is None:
                s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
                s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
                break
            if selection.tool_name not in READONLY_TOOLS:
                call = await _call_tool(gateway, s, selection.tool_name, selection.arguments)
                tool_calls.append(call)
                s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
                s["workflow_state"] = WorkflowState.NEEDS_HUMAN_INTERVENTION.value
                s["result"] = {**(s.get("result") or {}), "stop_reason": "model selected an unregistered tool"}
                break
            call = await _call_tool(gateway, s, selection.tool_name, selection.arguments)
            tool_calls.append(call)
            if call["status"] == "SUCCEEDED":
                import hashlib
                import json

                output_json = json.dumps(call["output"], sort_keys=True, default=str)
                evidence.append(
                    {
                        "evidence_id": repo.new_id("EVID"),
                        "run_id": s["run_id"],
                        "source": selection.tool_name,
                        "source_type": selection.tool_name.removeprefix("read_").removeprefix("inspect_"),
                        "tool_call_id": call["tool_call_id"],
                        "timestamp": _utcnow(),
                        "content": output_json,
                        "summary": selection.reason,
                        "hash": hashlib.sha256(output_json.encode()).hexdigest(),
                        "result": call["output"],
                    }
                )
            if len(tool_calls) >= settings.max_tool_calls:
                s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
                s["workflow_state"] = WorkflowState.NEEDS_HUMAN_INTERVENTION.value
                break

        s["tool_calls"] = tool_calls
        s["evidence"] = evidence
        s["result"] = {**(s.get("result") or {}), "investigation_steps": gateway.budget.step_count}
        if not gateway.budget.can_step() and s.get("status") == "RUNNING":
            s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
            s["workflow_state"] = WorkflowState.NEEDS_HUMAN_INTERVENTION.value
        if not evidence:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["result"] = {"message": "no evidence collected"}
        return s

    async def form_hypothesis_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.FORM_HYPOTHESIS.value
        if s.get("status") != AgentRunStatus.RUNNING.value:
            return s
        evidence = s.get("evidence") or []
        evidence = valid_evidence(evidence, s.get("tool_calls") or [], s["run_id"])
        s["evidence"] = evidence
        if len(evidence) < 2:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["result"] = {"message": "insufficient evidence", "evidence_count": len(evidence)}
            return s
        if settings.evaluation_strategy == "direct":
            conclusion = await decision_model.direct_conclusion(incident_context(s), evidence)
            ids = conclusion.supporting_evidence_ids
            if ids and set(ids).issubset({e["evidence_id"] for e in evidence}):
                s["root_cause"] = {**conclusion.model_dump(), "summary": conclusion.statement, "evidence_ids": ids, "verified": False}
                s["status"] = AgentRunStatus.INVESTIGATION_COMPLETE.value
                s["workflow_state"] = WorkflowState.ROOT_CAUSE_FOUND.value
            else:
                s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
                s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            return s
        proposal = await decision_model.propose_hypothesis(incident_context(s), evidence)
        known = {e["evidence_id"] for e in evidence}
        if not proposal.evidence_ids or not set(proposal.evidence_ids).issubset(known):
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["result"] = {"message": "model hypothesis cited unknown or no evidence"}
            return s
        s["hypotheses"] = list(s.get("hypotheses") or []) + [
            {**proposal.model_dump(), "status": "OPEN", "verified": False}
        ]
        return s

    async def verify_hypothesis_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.VERIFY_HYPOTHESIS.value
        if s.get("status") != AgentRunStatus.RUNNING.value:
            return s
        if not s.get("hypotheses"):
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s

        from incidentpilot.llm.schemas import HypothesisProposal

        proposal = HypothesisProposal.model_validate(s["hypotheses"][-1])
        if settings.evaluation_strategy == "fixed":
            from incidentpilot.llm.schemas import ToolSelection

            selection = ToolSelection(tool_name="read_source_code", arguments={"repository": s["repository"], "path": "app.py"}, reason="fixed independent verification")
        else:
            selection = await decision_model.verification_action(
                incident_context(s), list(s.get("evidence") or []), proposal
            )
        source = await _call_tool(gateway, s, selection.tool_name, selection.arguments)
        s["tool_calls"] = list(s.get("tool_calls") or []) + [source]
        extra_evidence = list(s.get("evidence") or [])
        if (source["status"] != "SUCCEEDED"
                or selection.tool_name in {e["source"] for e in extra_evidence}
                or content_hash(source["output"]) in {e.get("hash") for e in extra_evidence}):
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["result"] = {**(s.get("result") or {}), "stop_reason": "verification failed or supplied no independent observation"}
            return s
        if source["status"] == "SUCCEEDED":
            extra_evidence.append(
                {
                    "evidence_id": repo.new_id("EVID"),
                    "run_id": s["run_id"],
                    "hash": content_hash(source["output"]),
                    "timestamp": _utcnow(),
                    "source": selection.tool_name,
                    "source_type": "verification",
                    "tool_call_id": source["tool_call_id"],
                    "content": str(source["output"]),
                    "summary": selection.reason,
                    "result": source["output"],
                }
            )
        s["evidence"] = extra_evidence
        extra_evidence = valid_evidence(extra_evidence, s["tool_calls"], s["run_id"])
        s["evidence"] = extra_evidence

        decision = await decision_model.verify(proposal, extra_evidence)
        hyp = dict(s["hypotheses"][-1])
        hyp["evidence_ids"] = [e["evidence_id"] for e in extra_evidence]
        ok, notes = decision.hypothesis_supported, decision.notes
        hyp["verified"] = ok
        hyp["status"] = "VERIFIED" if ok else "REJECTED"
        hyp["verification_notes"] = notes
        s["hypotheses"] = list(s["hypotheses"][:-1]) + [hyp]
        if not ok:
            s["result"] = {"message": notes, "hypothesis": hyp}
            rejected_count = sum(
                1 for item in s["hypotheses"] if item.get("status") == "REJECTED"
            )
            if rejected_count >= 3 or not gateway.budget.can_step():
                s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
                s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s
        conclusion = await decision_model.conclude(
            incident_context(s),
            HypothesisProposal.model_validate(hyp),
            extra_evidence,
        )
        known = {e["evidence_id"] for e in extra_evidence}
        if (not conclusion.supporting_evidence_ids
                or not set(conclusion.supporting_evidence_ids).issubset(known)
                or extra_evidence[-1]["evidence_id"] not in conclusion.supporting_evidence_ids
                or len({e["source"] for e in extra_evidence if e["evidence_id"] in conclusion.supporting_evidence_ids}) < 2
                or (not decision_model.deterministic and conclusion.fault_category not in {
                    "database_performance", "application_bug", "dependency", "data_contract", "configuration", "code_regression"})):
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s
        s["root_cause"] = {
            "summary": conclusion.statement,
            "statement": conclusion.statement,
            "fault_category": conclusion.fault_category,
            "affected_component": conclusion.affected_component,
            "causal_facts": conclusion.causal_facts,
            "evidence_ids": conclusion.supporting_evidence_ids,
            "confidence": conclusion.confidence,
            "verified": True,
        }
        s["workflow_state"] = WorkflowState.ROOT_CAUSE_FOUND.value
        if settings.investigation_only:
            s["status"] = AgentRunStatus.INVESTIGATION_COMPLETE.value
        return s

    async def create_sandbox_node(s: AgentState) -> AgentState:
        from incidentpilot.sandbox.manager import SandboxManager

        s["workflow_state"] = WorkflowState.CREATE_SANDBOX.value
        if not s.get("root_cause"):
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s
        source = Path(settings.reference_repo_path)
        if not source.exists():
            source = Path(__file__).resolve().parents[3] / "reference" / "orders_api"
        manager = SandboxManager(settings)
        try:
            manager.create_workspace(s["run_id"], source)
            s["result"] = {**(s.get("result") or {}), "sandbox_source": str(source)}
            s["_sandbox_manager"] = manager  # type: ignore[typeddict-item]
        except Exception as exc:
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = f"sandbox create failed: {exc}"
            s["workflow_state"] = WorkflowState.FAILED.value
        return s

    async def generate_patch_node(s: AgentState) -> AgentState:
        from incidentpilot.sandbox.manager import generate_deterministic_patch

        s["workflow_state"] = WorkflowState.GENERATE_PATCH.value
        manager = s.get("_sandbox_manager")
        if manager is None:
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = "sandbox manager missing"
            return s
        summary = (s.get("root_cause") or {}).get("summary", "regression")
        attempts = list(s.get("patch_attempts") or [])
        attempts.append({"attempt": len(attempts) + 1, "summary": summary})
        s["patch_attempts"] = attempts
        if len(attempts) > settings.max_patch_attempts:
            s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
            s["workflow_state"] = WorkflowState.NEEDS_HUMAN_INTERVENTION.value
            s["error"] = "max patch attempts exceeded"
            return s
        patch = generate_deterministic_patch(summary)
        try:
            manager.apply_patch(s["run_id"], patch)
            s["patch_attempts"][-1]["patch_diff"] = patch
            s["patch_attempts"][-1]["status"] = "applied"
        except Exception as exc:
            s["patch_attempts"][-1]["status"] = "apply_failed"
            s["patch_attempts"][-1]["error"] = str(exc)
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = f"apply patch failed: {exc}"
        return s

    async def run_tests_node(s: AgentState) -> AgentState:
        from incidentpilot.models.enums import PatchAttemptStatus, TestRunStatus

        s["workflow_state"] = WorkflowState.RUN_TESTS.value
        manager = s.get("_sandbox_manager")
        if manager is None:
            s["status"] = AgentRunStatus.FAILED.value
            return s
        try:
            result = await manager.run_tests_async(s["run_id"], profile="pytest", timeout=90)
        except Exception as exc:
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = f"run tests failed: {exc}"
            return s
        test_run_id = f"TR-{uuid.uuid4().hex[:12]}"
        if s.get("patch_attempts"):
            s["patch_attempts"][-1].update(
                {
                    "test_run_id": test_run_id,
                    "test_ok": result.ok,
                    "test_exit_code": result.exit_code,
                    "test_duration_ms": result.duration_ms,
                    "base_commit_sha": manager.base_commit_sha(s["run_id"]),
                    "test_mode": result.mode,
                    "test_profile": "pytest_fault" if settings.sandbox_fault_type else "pytest_regression",
                    "test_stdout": result.stdout[-4000:],
                    "test_stderr": result.stderr[-2000:],
                    "status": (
                        PatchAttemptStatus.TESTS_PASSED.value
                        if result.ok
                        else PatchAttemptStatus.TESTS_FAILED.value
                    ),
                }
            )
        s["result"] = {
            **(s.get("result") or {}),
            "test_run_id": test_run_id,
            "test_status": (
                TestRunStatus.PASSED.value if result.ok else TestRunStatus.FAILED.value
            ),
            "test_mode": result.mode,
            "test_exit_code": result.exit_code,
        }
        s["_test_ok"] = result.ok  # type: ignore[typeddict-item]
        return s

    def route_after_tests(s: AgentState) -> str:
        if s.get("_test_ok"):
            return "export_patch_artifact"
        if len(s.get("patch_attempts") or []) < settings.max_patch_attempts:
            return "reflect"
        return "needs_human"

    async def reflect_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.REFLECT.value
        attempts = list(s.get("patch_attempts") or [])
        if attempts:
            attempts[-1]["reflection"] = "tests failed; retry with simplified patch"
        s["patch_attempts"] = attempts
        return s

    async def export_artifact_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.EXPORT_PATCH_ARTIFACT.value
        manager = s.get("_sandbox_manager")
        if manager is None:
            s["status"] = AgentRunStatus.FAILED.value
            return s
        patch = ""
        if s.get("patch_attempts"):
            patch = s["patch_attempts"][-1].get("patch_diff", "")
        test_run_id = (s.get("result") or {}).get("test_run_id", "")
        artifact = manager.export_patch_artifact(
            s["run_id"],
            base_commit_sha="local-dev",
            patch_diff=patch,
            test_run_id=test_run_id,
        )
        s["patch_artifact"] = artifact.to_dict()

        artifact.patch_artifact_id = await _persist_patch_artifact_once(s["run_id"], artifact)
        s["patch_artifact"] = artifact.to_dict()
        manager.destroy_workspace(s["run_id"])
        s["workflow_state"] = WorkflowState.WAIT_FOR_APPROVAL.value
        s["status"] = AgentRunStatus.WAITING_APPROVAL.value
        s["approval_required"] = True
        s["result"] = {
            **(s.get("result") or {}),
            "patch_artifact_id": artifact.patch_artifact_id,
            "patch_hash": artifact.patch_hash,
        }
        return s

    async def needs_human_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.NEEDS_HUMAN_INTERVENTION.value
        s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
        return s

    def route_after_collect(s: AgentState) -> str:
        if s.get("status") != AgentRunStatus.RUNNING.value:
            return "end"
        return "form_hypothesis"

    def route_after_verify(s: AgentState) -> str:
        if s.get("status") != AgentRunStatus.RUNNING.value:
            return "end"
        if s.get("root_cause"):
            if s.get("status") == AgentRunStatus.INVESTIGATION_COMPLETE.value:
                return "end"
            return "create_sandbox"
        if s.get("status") == AgentRunStatus.INSUFFICIENT_EVIDENCE.value:
            return "end"
        rejected = [h for h in (s.get("hypotheses") or []) if h.get("status") == "REJECTED"]
        if rejected and gateway.budget.can_step() and len(rejected) < 3:
            return "collect_evidence"
        return "end"

    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("collect_evidence", collect_evidence_node)
    builder.add_node("form_hypothesis", form_hypothesis_node)
    builder.add_node("verify_hypothesis", verify_hypothesis_node)
    builder.add_node("create_sandbox", create_sandbox_node)
    builder.add_node("generate_patch", generate_patch_node)
    builder.add_node("run_tests", run_tests_node)
    builder.add_node("reflect", reflect_node)
    builder.add_node("export_patch_artifact", export_artifact_node)
    builder.add_node("needs_human", needs_human_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "collect_evidence")
    builder.add_conditional_edges(
        "collect_evidence",
        route_after_collect,
        {"form_hypothesis": "form_hypothesis", "end": END},
    )
    builder.add_conditional_edges("form_hypothesis", lambda s: "verify" if s.get("status") == "RUNNING" else "end", {"verify": "verify_hypothesis", "end": END})
    builder.add_conditional_edges(
        "verify_hypothesis",
        route_after_verify,
        {
            "create_sandbox": "create_sandbox",
            "collect_evidence": "collect_evidence",
            "end": END,
        },
    )
    builder.add_conditional_edges("create_sandbox", lambda s: "generate_patch" if s.get("status") == "RUNNING" else "end", {"generate_patch": "generate_patch", "end": END})
    builder.add_conditional_edges(
        "generate_patch",
        lambda s: "run_tests"
        if s.get("status") == "RUNNING" and s.get("patch_attempts") and s["patch_attempts"][-1].get("status") == "applied"
        else "needs_human",
        {"run_tests": "run_tests", "needs_human": "needs_human"},
    )
    builder.add_conditional_edges(
        "run_tests",
        route_after_tests,
        {
            "end": END,
            "export_patch_artifact": "export_patch_artifact",
            "reflect": "reflect",
            "needs_human": "needs_human",
        },
    )
    builder.add_edge("reflect", "generate_patch")
    builder.add_edge("export_patch_artifact", END)
    builder.add_edge("needs_human", END)

    # LangGraph state is schema-bound; keep non-serializable runtime handles outside.
    runtime: dict[str, Any] = {}
    state["_runtime_key"] = state["run_id"]  # type: ignore[typeddict-item]

    async def plan_node_r(s: AgentState) -> AgentState:
        return await plan_node(s)

    # Rebind nodes to use runtime dict for manager/test flags
    async def create_sandbox_node_r(s: AgentState) -> AgentState:
        from incidentpilot.sandbox.manager import SandboxManager

        s["workflow_state"] = WorkflowState.CREATE_SANDBOX.value
        if not s.get("root_cause"):
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s
        source = Path(settings.reference_repo_path)
        if not source.exists():
            source = Path(__file__).resolve().parents[3] / "reference" / "orders_api"
        manager = SandboxManager(settings)
        runtime[s["run_id"]] = {"manager": manager}
        try:
            manager.create_workspace(s["run_id"], source)
            s["result"] = {**(s.get("result") or {}), "sandbox_source": str(source)}
        except Exception as exc:
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = f"sandbox create failed: {exc}"
            s["workflow_state"] = WorkflowState.FAILED.value
        return s

    async def ensure_workspace(s: AgentState, *, replay_patch=False):
        manager = (runtime.get(s["run_id"]) or {}).get("manager")
        if manager is not None:
            return manager
        await create_sandbox_node_r(s)
        if s.get("status") != "RUNNING":
            return None
        manager = runtime[s["run_id"]]["manager"]
        attempts = s.get("patch_attempts") or []
        if replay_patch and attempts and attempts[-1].get("patch_diff"):
            expected_base = attempts[-1].get("base_commit_sha")
            if expected_base != manager.base_commit_sha(s["run_id"]):
                raise ValueError("checkpoint patch base changed; revalidation required")
            manager.apply_patch(s["run_id"], attempts[-1]["patch_diff"])
        return manager

    async def generate_patch_node_r(s: AgentState) -> AgentState:
        from incidentpilot.sandbox.manager import generate_deterministic_patch

        s["workflow_state"] = WorkflowState.GENERATE_PATCH.value
        if s.get("patch_attempts"):
            old = (runtime.pop(s["run_id"], {}) or {}).get("manager")
            if old is not None:
                old.destroy_workspace(s["run_id"])
        manager = await ensure_workspace(s)
        if (manager is not None and s.get("patch_attempts")
                and s["patch_attempts"][0].get("base_commit_sha") != manager.base_commit_sha(s["run_id"])):
            raise ValueError("base changed between patch attempts")
        if manager is None:
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = "sandbox manager missing"
            return s
        summary = (s.get("root_cause") or {}).get("summary", "regression")
        attempts = list(s.get("patch_attempts") or [])
        attempts.append({"attempt": len(attempts) + 1, "summary": summary})
        if len(attempts) > settings.max_patch_attempts:
            s["patch_attempts"] = attempts
            s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
            s["workflow_state"] = WorkflowState.NEEDS_HUMAN_INTERVENTION.value
            s["error"] = "max patch attempts exceeded"
            return s
        s["patch_attempts"] = attempts
        if decision_model.deterministic:
            patch = generate_deterministic_patch(summary)
            patch_generator = "deterministic-test-adapter"
        else:
            proposal = await decision_model.patch(
                {
                    "root_cause": s.get("root_cause"),
                    "supporting_evidence": s.get("evidence"),
                    "previous_attempts": attempts[:-1],
                    "repository": s.get("repository"),
                }
            )
            patch = proposal.unified_diff
            patch_generator = f"{provider.provider_name}:{provider.model_name}"
        try:
            manager.apply_patch(s["run_id"], patch)
            s["patch_attempts"][-1]["patch_diff"] = patch
            s["patch_attempts"][-1]["status"] = "applied"
            s["patch_attempts"][-1]["generator"] = patch_generator
            s["patch_attempts"][-1]["base_commit_sha"] = manager.base_commit_sha(s["run_id"])
        except Exception as exc:
            s["patch_attempts"][-1]["status"] = "apply_failed"
            s["patch_attempts"][-1]["error"] = str(exc)
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = f"apply patch failed: {exc}"
        return s

    async def run_tests_node_r(s: AgentState) -> AgentState:
        from incidentpilot.models.enums import PatchAttemptStatus, TestRunStatus

        s["workflow_state"] = WorkflowState.RUN_TESTS.value
        manager = await ensure_workspace(s, replay_patch=True)
        if manager is None:
            s["status"] = AgentRunStatus.FAILED.value
            return s
        try:
            result = await manager.run_tests_async(s["run_id"], profile="pytest_fault" if settings.sandbox_fault_type else "pytest_regression", timeout=90)
        except Exception as exc:
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = f"run tests failed: {exc}"
            return s
        test_run_id = f"TR-{uuid.uuid4().hex[:12]}"
        if s.get("patch_attempts"):
            s["patch_attempts"][-1].update(
                {
                    "test_run_id": test_run_id,
                    "test_ok": result.ok,
                    "test_exit_code": result.exit_code,
                    "test_duration_ms": result.duration_ms,
                    "base_commit_sha": manager.base_commit_sha(s["run_id"]),
                    "test_mode": result.mode,
                    "test_profile": "pytest_fault" if settings.sandbox_fault_type else "pytest_regression",
                    "test_stdout": result.stdout[-4000:],
                    "test_stderr": result.stderr[-2000:],
                    "status": (
                        PatchAttemptStatus.TESTS_PASSED.value
                        if result.ok
                        else PatchAttemptStatus.TESTS_FAILED.value
                    ),
                }
            )
        s["result"] = {
            **(s.get("result") or {}),
            "test_run_id": test_run_id,
            "test_status": (
                TestRunStatus.PASSED.value if result.ok else TestRunStatus.FAILED.value
            ),
            "test_mode": result.mode,
            "test_scope": "fault-active" if settings.sandbox_fault_type else "healthy-regression-only",
            "test_exit_code": result.exit_code,
        }
        runtime.setdefault(s["run_id"], {})["test_ok"] = result.ok
        return s

    def route_after_tests_r(s: AgentState) -> str:
        if s.get("status") != AgentRunStatus.RUNNING.value:
            return "end"
        if runtime.get(s["run_id"], {}).get("test_ok"):
            return "export_patch_artifact"
        if len(s.get("patch_attempts") or []) < settings.max_patch_attempts:
            return "reflect"
        return "needs_human"

    async def export_artifact_node_r(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.EXPORT_PATCH_ARTIFACT.value
        if not (runtime.get(s["run_id"]) or {}).get("manager"):
            await run_tests_node_r(s)
            if s.get("status") != "RUNNING" or not runtime.get(s["run_id"], {}).get("test_ok"):
                s["status"] = "FAILED"
                s["workflow_state"] = WorkflowState.FAILED.value
                return s
        manager = (runtime.get(s["run_id"]) or {}).get("manager")
        if manager is None:
            s["status"] = AgentRunStatus.FAILED.value
            return s
        patch = ""
        if s.get("patch_attempts"):
            patch = s["patch_attempts"][-1].get("patch_diff", "")
        test_run_id = (s.get("result") or {}).get("test_run_id", "")
        artifact = manager.export_patch_artifact(
            s["run_id"],
            base_commit_sha=manager.base_commit_sha(s["run_id"]),
            patch_diff=patch,
            test_run_id=test_run_id,
        )
        s["patch_artifact"] = artifact.to_dict()

        artifact.patch_artifact_id = await _persist_patch_artifact_once(s["run_id"], artifact)
        s["patch_artifact"] = artifact.to_dict()
        manager.destroy_workspace(s["run_id"])
        s["workflow_state"] = WorkflowState.WAIT_FOR_APPROVAL.value
        s["status"] = AgentRunStatus.WAITING_APPROVAL.value
        s["approval_required"] = True
        s["result"] = {
            **(s.get("result") or {}),
            "patch_artifact_id": artifact.patch_artifact_id,
            "patch_hash": artifact.patch_hash,
        }
        return s

    async def wait_for_approval_node(s: AgentState) -> AgentState:
        from langgraph.types import interrupt

        decision = interrupt(
            {
                "run_id": s["run_id"],
                "patch_artifact": s.get("patch_artifact"),
                "root_cause": s.get("root_cause"),
            }
        )
        from incidentpilot.agent.approval_resume import resume_after_approval
        from incidentpilot.models.enums import ApprovalDecision

        outcome = await resume_after_approval(
            run_id=s["run_id"],
            decision=ApprovalDecision(str(decision)),
            settings=settings,
        )
        s["result"] = {**(s.get("result") or {}), **outcome}
        s["status"] = outcome["status"]
        s["workflow_state"] = (
            WorkflowState.RESOLVED.value
            if outcome["status"] == AgentRunStatus.RESOLVED.value
            else WorkflowState.NEEDS_HUMAN_INTERVENTION.value
        )
        return s

    def guarded(node, *, approval=False):
        async def invoke(s: AgentState):
            from incidentpilot.persistence.ownership import fence

            if not approval and s.get("status") not in {None, "RUNNING"}:
                return s
            async with get_session_factory()() as guard_session:
                await fence(guard_session, s["run_id"])
                await guard_session.commit()
            gateway.budget.tool_call_count = max(gateway.budget.tool_call_count, sum(c.get("attempt_count", 1) for c in (s.get("tool_calls") or [])))
            gateway.budget.step_count = max(gateway.budget.step_count, (s.get("result") or {}).get("investigation_steps", 0))
            outcome = await node(s)
            if not approval:
                async with get_session_factory()() as guard_session:
                    from sqlalchemy import update

                    from incidentpilot.models.db import AgentRun

                    await fence(guard_session, s["run_id"])
                    await guard_session.execute(update(AgentRun).where(AgentRun.run_id == s["run_id"]).values(
                        workflow_state=outcome.get("workflow_state", "PLAN"),
                        plan=outcome.get("plan") or [],
                        tool_call_count=len(outcome.get("tool_calls") or []),
                    ))
                    await guard_session.commit()
            return outcome
        return invoke

    builder = StateGraph(AgentState)
    builder.add_node("plan", guarded(plan_node))
    builder.add_node("collect_evidence", guarded(collect_evidence_node))
    builder.add_node("form_hypothesis", guarded(form_hypothesis_node))
    builder.add_node("verify_hypothesis", guarded(verify_hypothesis_node))
    builder.add_node("create_sandbox", guarded(create_sandbox_node_r))
    builder.add_node("generate_patch", guarded(generate_patch_node_r))
    builder.add_node("run_tests", guarded(run_tests_node_r))
    builder.add_node("reflect", guarded(reflect_node))
    builder.add_node("export_patch_artifact", guarded(export_artifact_node_r))
    builder.add_node("wait_for_approval", guarded(wait_for_approval_node, approval=True))
    builder.add_node("needs_human", guarded(needs_human_node))

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "collect_evidence")
    builder.add_conditional_edges(
        "collect_evidence",
        route_after_collect,
        {"form_hypothesis": "form_hypothesis", "end": END},
    )
    builder.add_conditional_edges("form_hypothesis", lambda s: "verify" if s.get("status") == "RUNNING" else "end", {"verify": "verify_hypothesis", "end": END})
    builder.add_conditional_edges(
        "verify_hypothesis",
        route_after_verify,
        {
            "create_sandbox": "create_sandbox",
            "collect_evidence": "collect_evidence",
            "end": END,
        },
    )
    builder.add_conditional_edges("create_sandbox", lambda s: "generate_patch" if s.get("status") == "RUNNING" else "end", {"generate_patch": "generate_patch", "end": END})
    builder.add_conditional_edges(
        "generate_patch",
        lambda s: "run_tests"
        if s.get("status") == "RUNNING" and s.get("patch_attempts") and s["patch_attempts"][-1].get("status") == "applied"
        else "needs_human",
        {"run_tests": "run_tests", "needs_human": "needs_human"},
    )
    builder.add_conditional_edges(
        "run_tests",
        route_after_tests_r,
        {
            "end": END,
            "export_patch_artifact": "export_patch_artifact",
            "reflect": "reflect",
            "needs_human": "needs_human",
        },
    )
    builder.add_edge("reflect", "generate_patch")
    builder.add_edge("export_patch_artifact", "wait_for_approval")
    builder.add_edge("wait_for_approval", END)
    builder.add_edge("needs_human", END)

    config = {
        "configurable": {"thread_id": state["run_id"]},
        "recursion_limit": 50,
    }
    invoke_input: Any = dict(state)
    if recover:
        invoke_input = None
    if resume_value is not None:
        from langgraph.types import Command

        invoke_input = Command(resume=resume_value)
    try:
        if settings.database_url.startswith("postgresql"):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            checkpoint_url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
            async with AsyncPostgresSaver.from_conn_string(checkpoint_url) as checkpointer:
                await checkpointer.setup()
                if resume_value is not None and await checkpointer.aget_tuple(config) is None:
                    raise ValueError("approval checkpoint missing; automatic resume denied")
                if recover and resume_value is None:
                    checkpoint = await checkpointer.aget_tuple(config)
                    if checkpoint is None:
                        invoke_input = dict(state)
                graph = builder.compile(checkpointer=checkpointer)
                result = await graph.ainvoke(invoke_input, config=config)
        else:
            # Explicit test-only path; formal runtime uses PostgreSQL checkpoints.
            global _TEST_CHECKPOINTER
            if _TEST_CHECKPOINTER is None:
                _TEST_CHECKPOINTER = MemorySaver()
            if resume_value is not None and await _TEST_CHECKPOINTER.aget_tuple(config) is None:
                raise ValueError("approval checkpoint missing; SQLite memory checkpoints do not survive restart")
            if recover and resume_value is None:
                checkpoint = await _TEST_CHECKPOINTER.aget_tuple(config)
                if checkpoint is None:
                    invoke_input = dict(state)
            graph = builder.compile(checkpointer=_TEST_CHECKPOINTER)
            result = await graph.ainvoke(invoke_input, config=config)
    finally:
        for handles in runtime.values():
            handles["manager"].destroy_workspace(state["run_id"])
    result["result"] = {**(result.get("result") or {}), "token_usage": getattr(provider, "usage_totals", None)}
    return dict(result)


async def resume_agent_workflow(
    *, run_id: str, decision: str, settings: Settings
) -> dict[str, Any]:
    """Resume the persisted LangGraph interrupt after an approval decision."""
    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import select

        from incidentpilot.models.db import AgentRun, Incident

        result = await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        incident = await session.get(Incident, run.incident_pk)
        if incident is None:
            raise ValueError(f"incident missing for run: {run_id}")
        state: AgentState = {
            "run_id": run.run_id,
            "incident_id": incident.incident_id,
            "incident_title": incident.title,
            "service": incident.service,
            "symptom": incident.symptom,
            "environment": incident.environment,
            "repository": incident.repository,
        }
    gateway, adapter = build_tool_gateway(settings)
    if hasattr(adapter, "connect"):
        await adapter.connect()
    try:
        from incidentpilot.persistence.ownership import workflow_lock

        async with workflow_lock(run_id, settings):
            return await _run_with_langgraph(state, settings, gateway, resume_value=decision)
    finally:
        if hasattr(adapter, "disconnect"):
            await adapter.disconnect()
