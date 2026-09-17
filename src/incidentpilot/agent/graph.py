"""LangGraph agent workflow with Tool Gateway and Sandbox integration."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from incidentpilot.agent.decision import READONLY_TOOLS, AgentDecisionModel
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

        adapter = MCPReadonlyAdapter(live=True)
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

        if hasattr(_adapter, "connect"):
            await _adapter.connect()
        try:
            final_state = await _execute_graph(state, settings, gateway, recover=recover)
        finally:
            if hasattr(_adapter, "disconnect"):
                await _adapter.disconnect()

        status = AgentRunStatus(final_state.get("status", AgentRunStatus.FAILED.value))

        # Persist investigation artifacts BEFORE marking terminal status so
        # API consumers never observe a finished run without evidence/tool calls.
        for item in final_state.get("tool_calls") or []:
            session.add(
                ToolCall(
                    tool_call_id=item.get("tool_call_id") or repo.new_id("TC"),
                    run_pk=run.id,
                    tool_name=item.get("tool_name", "unknown"),
                    input=item.get("input") or {},
                    output=item.get("output") or {},
                    status=item.get("status", "SUCCEEDED"),
                    error=item.get("error"),
                    latency_ms=item.get("latency_ms"),
                )
            )
        for item in final_state.get("evidence") or []:
            session.add(
                Evidence(
                    evidence_id=item.get("evidence_id") or repo.new_id("EVID"),
                    run_pk=run.id,
                    source=item.get("source", ""),
                    source_type=item.get("source_type", "other"),
                    tool_call_id=item.get("tool_call_id"),
                    content=item.get("content", ""),
                    summary=item.get("summary", ""),
                    content_hash=item.get("hash", ""),
                    result=item.get("result") or {},
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
            if item.get("test_run_id"):
                session.add(
                    TestRun(
                        test_run_id=item["test_run_id"],
                        run_pk=run.id,
                        status="PASSED" if item.get("test_ok") else "FAILED",
                        exit_code=(final_state.get("result") or {}).get("test_exit_code"),
                        stdout=item.get("test_stdout", ""),
                        stderr=item.get("test_stderr", ""),
                        command="pytest_regression",
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
            result=final_state.get("result") or {},
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
        return await _run_with_langgraph(state, settings, gateway, recover=recover)
    except Exception as exc:
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
        plan = await decision_model.plan(incident_context(s))
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
        if not evidence:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["result"] = {"message": "no evidence collected"}
        return s

    async def form_hypothesis_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.FORM_HYPOTHESIS.value
        if s.get("status") == AgentRunStatus.INSUFFICIENT_EVIDENCE.value:
            return s
        evidence = s.get("evidence") or []
        if len(evidence) < 2:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["result"] = {"message": "insufficient evidence", "evidence_count": len(evidence)}
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
        if s.get("status") == AgentRunStatus.INSUFFICIENT_EVIDENCE.value:
            return s
        if not s.get("hypotheses"):
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s

        from incidentpilot.llm.schemas import HypothesisProposal

        proposal = HypothesisProposal.model_validate(s["hypotheses"][-1])
        selection = await decision_model.verification_action(
            incident_context(s), list(s.get("evidence") or []), proposal
        )
        source = await _call_tool(gateway, s, selection.tool_name, selection.arguments)
        s["tool_calls"] = list(s.get("tool_calls") or []) + [source]
        extra_evidence = list(s.get("evidence") or [])
        if source["status"] == "SUCCEEDED":
            extra_evidence.append(
                {
                    "evidence_id": repo.new_id("EVID"),
                    "source": selection.tool_name,
                    "source_type": "verification",
                    "tool_call_id": source["tool_call_id"],
                    "content": str(source["output"]),
                    "summary": selection.reason,
                    "result": source["output"],
                }
            )
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
        if not conclusion.supporting_evidence_ids or not set(conclusion.supporting_evidence_ids).issubset(known):
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
            result = manager.run_tests(s["run_id"], profile="pytest", timeout=90)
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

        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            from incidentpilot.models.db import AgentRun, PatchArtifact

            run_result = await session.execute(
                select(AgentRun).where(AgentRun.run_id == s["run_id"])
            )
            run = run_result.scalar_one_or_none()
            if run is not None:
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
        if s.get("status") == AgentRunStatus.INSUFFICIENT_EVIDENCE.value:
            return "end"
        return "form_hypothesis"

    def route_after_verify(s: AgentState) -> str:
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
    builder.add_edge("form_hypothesis", "verify_hypothesis")
    builder.add_conditional_edges(
        "verify_hypothesis",
        route_after_verify,
        {
            "create_sandbox": "create_sandbox",
            "collect_evidence": "collect_evidence",
            "end": END,
        },
    )
    builder.add_edge("create_sandbox", "generate_patch")
    builder.add_conditional_edges(
        "generate_patch",
        lambda s: "run_tests"
        if s.get("patch_attempts") and s["patch_attempts"][-1].get("status") == "applied"
        else "needs_human",
        {"run_tests": "run_tests", "needs_human": "needs_human"},
    )
    builder.add_conditional_edges(
        "run_tests",
        route_after_tests,
        {
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
        try:
            manager.create_workspace(s["run_id"], source)
            runtime[s["run_id"]] = {"manager": manager}
            s["result"] = {**(s.get("result") or {}), "sandbox_source": str(source)}
        except Exception as exc:
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = f"sandbox create failed: {exc}"
            s["workflow_state"] = WorkflowState.FAILED.value
        return s

    async def generate_patch_node_r(s: AgentState) -> AgentState:
        from incidentpilot.sandbox.manager import generate_deterministic_patch

        s["workflow_state"] = WorkflowState.GENERATE_PATCH.value
        manager = (runtime.get(s["run_id"]) or {}).get("manager")
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
        except Exception as exc:
            s["patch_attempts"][-1]["status"] = "apply_failed"
            s["patch_attempts"][-1]["error"] = str(exc)
            s["status"] = AgentRunStatus.FAILED.value
            s["error"] = f"apply patch failed: {exc}"
        return s

    async def run_tests_node_r(s: AgentState) -> AgentState:
        from incidentpilot.models.enums import PatchAttemptStatus, TestRunStatus

        s["workflow_state"] = WorkflowState.RUN_TESTS.value
        manager = (runtime.get(s["run_id"]) or {}).get("manager")
        if manager is None:
            s["status"] = AgentRunStatus.FAILED.value
            return s
        try:
            result = manager.run_tests(s["run_id"], profile="pytest_regression", timeout=90)
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
        runtime.setdefault(s["run_id"], {})["test_ok"] = result.ok
        return s

    def route_after_tests_r(s: AgentState) -> str:
        if runtime.get(s["run_id"], {}).get("test_ok"):
            return "export_patch_artifact"
        if len(s.get("patch_attempts") or []) < settings.max_patch_attempts:
            return "reflect"
        return "needs_human"

    async def export_artifact_node_r(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.EXPORT_PATCH_ARTIFACT.value
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

        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            from incidentpilot.models.db import AgentRun, PatchArtifact

            run_result = await session.execute(
                select(AgentRun).where(AgentRun.run_id == s["run_id"])
            )
            run = run_result.scalar_one_or_none()
            if run is not None:
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

    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("collect_evidence", collect_evidence_node)
    builder.add_node("form_hypothesis", form_hypothesis_node)
    builder.add_node("verify_hypothesis", verify_hypothesis_node)
    builder.add_node("create_sandbox", create_sandbox_node_r)
    builder.add_node("generate_patch", generate_patch_node_r)
    builder.add_node("run_tests", run_tests_node_r)
    builder.add_node("reflect", reflect_node)
    builder.add_node("export_patch_artifact", export_artifact_node_r)
    builder.add_node("wait_for_approval", wait_for_approval_node)
    builder.add_node("needs_human", needs_human_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "collect_evidence")
    builder.add_conditional_edges(
        "collect_evidence",
        route_after_collect,
        {"form_hypothesis": "form_hypothesis", "end": END},
    )
    builder.add_edge("form_hypothesis", "verify_hypothesis")
    builder.add_conditional_edges(
        "verify_hypothesis",
        route_after_verify,
        {
            "create_sandbox": "create_sandbox",
            "collect_evidence": "collect_evidence",
            "end": END,
        },
    )
    builder.add_edge("create_sandbox", "generate_patch")
    builder.add_conditional_edges(
        "generate_patch",
        lambda s: "run_tests"
        if s.get("patch_attempts") and s["patch_attempts"][-1].get("status") == "applied"
        else "needs_human",
        {"run_tests": "run_tests", "needs_human": "needs_human"},
    )
    builder.add_conditional_edges(
        "run_tests",
        route_after_tests_r,
        {
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
    if settings.database_url.startswith("postgresql"):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpoint_url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
        async with AsyncPostgresSaver.from_conn_string(checkpoint_url) as checkpointer:
            await checkpointer.setup()
            graph = builder.compile(checkpointer=checkpointer)
            result = await graph.ainvoke(invoke_input, config=config)
    else:
        # Explicit test-only path; formal runtime uses PostgreSQL checkpoints.
        global _TEST_CHECKPOINTER
        if _TEST_CHECKPOINTER is None:
            _TEST_CHECKPOINTER = MemorySaver()
        graph = builder.compile(checkpointer=_TEST_CHECKPOINTER)
        result = await graph.ainvoke(invoke_input, config=config)
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
        return await _run_with_langgraph(
            state, settings, gateway, resume_value=decision
        )
    finally:
        if hasattr(adapter, "disconnect"):
            await adapter.disconnect()
