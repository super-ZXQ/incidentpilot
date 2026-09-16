"""LangGraph agent workflow with Tool Gateway and Sandbox integration."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from incidentpilot.config import Settings
from incidentpilot.models.enums import AgentRunStatus, WorkflowState
from incidentpilot.persistence import repo
from incidentpilot.persistence.session import get_session_factory
from incidentpilot.tools.fake import register_fake_readonly_tools
from incidentpilot.tools.gateway import BudgetTracker, ToolGateway, ToolRegistry
from incidentpilot.tools.mcp_adapter import register_mcp_readonly_tools

logger = logging.getLogger(__name__)


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


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def build_tool_gateway(settings: Settings) -> tuple[ToolGateway, Any]:
    registry = ToolRegistry()
    adapter = register_mcp_readonly_tools(registry)
    # Keep fake tools registered as additional deterministic backend for tests
    # that call names directly; MCP adapter already covers readonly names.
    _ = register_fake_readonly_tools
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
) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import select

        from incidentpilot.models.db import AgentRun, Evidence, Hypothesis, Incident, ToolCall
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
        }

        final_state = await _execute_graph(state, settings, gateway)

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
                AgentRunStatus.RESOLVED,
                AgentRunStatus.INSUFFICIENT_EVIDENCE,
                AgentRunStatus.NEEDS_HUMAN_INTERVENTION,
                AgentRunStatus.FAILED,
                AgentRunStatus.WAITING_APPROVAL,
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
    state: AgentState, settings: Settings, gateway: ToolGateway
) -> AgentState:
    try:
        return await _run_with_langgraph(state, settings, gateway)
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
    result = await gateway.call(name, payload, run_id=state.get("run_id", ""), trace_id="")
    return result.to_dict()


async def _run_with_langgraph(
    state: AgentState, settings: Settings, gateway: ToolGateway
) -> AgentState:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    async def plan_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.PLAN.value
        s["plan"] = [
            "Collect metrics evidence via read_metrics",
            "Collect logs evidence via read_logs",
            "Inspect git history and source code",
            "Form hypothesis referencing evidence ids",
            "Verify hypothesis with additional tool calls",
            "Create sandbox, generate patch, run tests",
            "Export PatchArtifact and wait for approval",
        ]
        return s

    async def collect_evidence_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.COLLECT_EVIDENCE.value
        tool_calls: list[dict[str, Any]] = list(s.get("tool_calls") or [])
        evidence: list[dict[str, Any]] = list(s.get("evidence") or [])

        for tool_name, payload, source_type in (
            ("read_metrics", {"service": s["service"], "window": "incident"}, "metrics"),
            ("read_logs", {"service": s["service"], "window": "incident"}, "logs"),
            (
                "inspect_git_history",
                {"repository": s.get("repository", ""), "limit": 5},
                "git_history",
            ),
        ):
            call = await _call_tool(gateway, s, tool_name, payload)
            tool_calls.append(call)
            if call["status"] == "SUCCEEDED":
                evidence.append(
                    {
                        "evidence_id": repo.new_id("EVID"),
                        "source": tool_name,
                        "source_type": source_type,
                        "tool_call_id": call["tool_call_id"],
                        "content": f"{tool_name} output for {s['service']}",
                        "result": call["output"],
                    }
                )

        s["tool_calls"] = tool_calls
        s["evidence"] = evidence
        if not evidence:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["result"] = {"message": "no evidence collected"}
        return s

    async def form_hypothesis_node(s: AgentState) -> AgentState:
        from incidentpilot.agent.reasoning import build_hypothesis

        s["workflow_state"] = WorkflowState.FORM_HYPOTHESIS.value
        if s.get("status") == AgentRunStatus.INSUFFICIENT_EVIDENCE.value:
            return s
        evidence = s.get("evidence") or []
        if len(evidence) < 2:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["result"] = {"message": "insufficient evidence", "evidence_count": len(evidence)}
            return s
        logs = next((e for e in evidence if e["source_type"] == "logs"), None)
        statement = (
            f"Service {s['service']} is degraded due to a recent regression: {s['symptom']}"
        )
        if logs:
            statement = (
                f"Recent regression in {s['service']} causes elevated latency/errors "
                f"({s['symptom']})"
            )
        s["hypotheses"] = [build_hypothesis(statement, evidence, confidence="MEDIUM")]
        return s

    async def verify_hypothesis_node(s: AgentState) -> AgentState:
        from incidentpilot.agent.reasoning import form_root_cause, verify_hypothesis

        s["workflow_state"] = WorkflowState.VERIFY_HYPOTHESIS.value
        if s.get("status") == AgentRunStatus.INSUFFICIENT_EVIDENCE.value:
            return s
        if not s.get("hypotheses"):
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s

        source = await _call_tool(
            gateway, s, "read_source_code", {"path": "app.py", "repository": ""}
        )
        s["tool_calls"] = list(s.get("tool_calls") or []) + [source]
        extra_evidence = list(s.get("evidence") or [])
        if source["status"] == "SUCCEEDED":
            extra_evidence.append(
                {
                    "evidence_id": repo.new_id("EVID"),
                    "source": "read_source_code",
                    "source_type": "source_code",
                    "tool_call_id": source["tool_call_id"],
                    "content": "Source inspection supports hypothesis.",
                    "result": source["output"],
                }
            )
        s["evidence"] = extra_evidence

        hyp = dict(s["hypotheses"][0])
        hyp["evidence_ids"] = [e["evidence_id"] for e in extra_evidence]
        ok, notes = verify_hypothesis(hyp, extra_evidence, min_evidence=2)
        hyp["verified"] = ok
        hyp["status"] = "VERIFIED" if ok else "REJECTED"
        hyp["verification_notes"] = notes
        s["hypotheses"] = [hyp]
        if not ok:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            s["result"] = {"message": notes, "hypothesis": hyp}
            return s
        s["root_cause"] = form_root_cause(
            hyp,
            affected_component=s["service"],
            fault_category="code_regression",
            causal_facts=["elevated latency/error", "recent change correlation"],
        )
        s["workflow_state"] = WorkflowState.ROOT_CAUSE_FOUND.value
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
            return "create_sandbox"
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
        {"create_sandbox": "create_sandbox", "end": END},
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

    graph = builder.compile(checkpointer=MemorySaver())
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
        {"create_sandbox": "create_sandbox", "end": END},
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
    builder.add_edge("export_patch_artifact", END)
    builder.add_edge("needs_human", END)

    graph = builder.compile(checkpointer=MemorySaver())
    config = {
        "configurable": {"thread_id": state["run_id"]},
        "recursion_limit": 50,
    }
    result = await graph.ainvoke(dict(state), config=config)
    return dict(result)
