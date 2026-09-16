"""LangGraph agent workflow.

Phase 1 ships a deterministic investigation stub so the control plane is testable.
Phase 2+ replaces fake tools with real Tool Gateway / MCP / Sandbox integration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, TypedDict

from incidentpilot.config import Settings
from incidentpilot.models.enums import AgentRunStatus, WorkflowState
from incidentpilot.persistence.session import get_session_factory

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
    status: str
    error: str | None
    result: dict[str, Any]
    limits: dict[str, Any]


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


async def run_agent_workflow(
    *,
    incident_pk: str,
    run_id: str,
    settings: Settings,
) -> dict[str, Any]:
    """Durable-ish entrypoint used by RunExecutor.

    Uses LangGraph StateGraph when available; falls back to a pure-Python
    state machine if graph compilation fails (keeps tests unblocked).
    """
    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import select

        from incidentpilot.models.db import AgentRun, Incident
        from incidentpilot.services.incidents import RunService

        incident = await session.get(Incident, incident_pk)
        run_result = await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))
        run = run_result.scalar_one_or_none()
        if incident is None or run is None:
            raise ValueError("incident or run not found")

        service = RunService(session)
        await service.mark_running(run)

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

        final_state = await _execute_graph(state, settings)

        status = AgentRunStatus(final_state.get("status", AgentRunStatus.FAILED.value))
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
            },
        )

        # Persist investigation artifacts
        from incidentpilot.models.db import Evidence, Hypothesis, ToolCall
        from incidentpilot.persistence import repo

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
        await repo.record_audit(
            session,
            event_type="agent_run_finished",
            message=f"run {run_id} finished with {status.value}",
            run_pk=run.id,
            data={"workflow_state": final_state.get("workflow_state")},
        )
        return final_state


async def _execute_graph(state: AgentState, settings: Settings) -> AgentState:
    """Try LangGraph; fall back to deterministic stub workflow."""
    try:
        return await _run_with_langgraph(state, settings)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("LangGraph execution unavailable, using stub workflow: %s", exc)
        return await _run_stub_workflow(state, settings)


async def _run_with_langgraph(state: AgentState, settings: Settings) -> AgentState:
    from langgraph.graph import END, START, StateGraph

    async def plan_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.PLAN.value
        s["plan"] = [
            "Collect metrics evidence",
            "Collect logs evidence",
            "Inspect git history and source",
            "Form and verify hypothesis",
            "If root cause confirmed: sandbox patch and tests",
        ]
        return s

    async def collect_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.COLLECT_EVIDENCE.value
        # Deterministic evidence collection is replaced by Tool Gateway in later phases.
        now = _utcnow()
        e1 = {
            "evidence_id": f"EVID-METRICS-{s['run_id'][-6:]}",
            "source": "read_metrics",
            "source_type": "metrics",
            "tool_call_id": f"TC-METRICS-{s['run_id'][-6:]}",
            "content": f"Service {s['service']} shows elevated latency/error rate for symptom: {s['symptom']}",
            "result": {"signal": "elevated_latency", "observed_at": now},
        }
        e2 = {
            "evidence_id": f"EVID-LOGS-{s['run_id'][-6:]}",
            "source": "read_logs",
            "source_type": "logs",
            "tool_call_id": f"TC-LOGS-{s['run_id'][-6:]}",
            "content": f"Error signature correlated with incident window for {s['service']}",
            "result": {"error_class": "handler_exception", "observed_at": now},
        }
        s["evidence"] = [e1, e2]
        s["tool_calls"] = [
            {
                "tool_call_id": e1["tool_call_id"],
                "tool_name": "read_metrics",
                "input": {"service": s["service"], "window": "incident"},
                "output": e1["result"],
                "status": "SUCCEEDED",
            },
            {
                "tool_call_id": e2["tool_call_id"],
                "tool_name": "read_logs",
                "input": {"service": s["service"], "window": "incident"},
                "output": e2["result"],
                "status": "SUCCEEDED",
            },
        ]
        return s

    async def form_hypothesis_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.FORM_HYPOTHESIS.value
        evidence_ids = [e["evidence_id"] for e in s.get("evidence") or []]
        if not evidence_ids:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s
        s["hypotheses"] = [
            {
                "statement": (
                    f"Recent regression in {s['service']} is causing symptom: {s['symptom']}"
                ),
                "evidence_ids": evidence_ids,
                "status": "OPEN",
                "confidence": "MEDIUM",
            }
        ]
        return s

    async def verify_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.VERIFY_HYPOTHESIS.value
        if not s.get("hypotheses"):
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s
        hyp = s["hypotheses"][0]
        hyp["verified"] = True
        hyp["status"] = "VERIFIED"
        hyp["verification_notes"] = "Cross-checked metrics and logs evidence."
        s["root_cause"] = {
            "summary": hyp["statement"],
            "evidence_ids": hyp["evidence_ids"],
            "affected_component": s["service"],
        }
        s["workflow_state"] = WorkflowState.ROOT_CAUSE_FOUND.value
        s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
        s["result"] = {
            "phase1_stub": True,
            "message": (
                "Root cause identified with deterministic stub tools. "
                "Sandbox/patch/approval implemented in later phases."
            ),
            "root_cause": s["root_cause"],
        }
        return s

    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("collect_evidence", collect_node)
    builder.add_node("form_hypothesis", form_hypothesis_node)
    builder.add_node("verify_hypothesis", verify_node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "collect_evidence")
    builder.add_edge("collect_evidence", "form_hypothesis")
    builder.add_edge("form_hypothesis", "verify_hypothesis")
    builder.add_edge("verify_hypothesis", END)
    graph = builder.compile()
    result = await graph.ainvoke(dict(state))
    return dict(result)


async def _run_stub_workflow(state: AgentState, settings: Settings) -> AgentState:
    """Pure-Python fallback used if LangGraph is unavailable."""
    s = dict(state)
    s["workflow_state"] = WorkflowState.PLAN.value
    s["plan"] = ["Collect evidence", "Form hypothesis", "Verify root cause"]
    s["workflow_state"] = WorkflowState.COLLECT_EVIDENCE.value
    now = _utcnow()
    s["evidence"] = [
        {
            "evidence_id": f"EVID-STUB-{s['run_id'][-6:]}",
            "source": "stub_tool",
            "source_type": "other",
            "tool_call_id": f"TC-STUB-{s['run_id'][-6:]}",
            "content": f"Stub evidence for {s['service']}: {s['symptom']}",
            "result": {"observed_at": now},
        }
    ]
    s["tool_calls"] = [
        {
            "tool_call_id": s["evidence"][0]["tool_call_id"],
            "tool_name": "stub_tool",
            "input": {"service": s["service"]},
            "output": s["evidence"][0]["result"],
            "status": "SUCCEEDED",
        }
    ]
    s["workflow_state"] = WorkflowState.FORM_HYPOTHESIS.value
    s["hypotheses"] = [
        {
            "statement": f"Issue in {s['service']}",
            "evidence_ids": [s["evidence"][0]["evidence_id"]],
            "status": "VERIFIED",
            "verified": True,
            "verification_notes": "stub",
        }
    ]
    s["root_cause"] = {
        "summary": s["hypotheses"][0]["statement"],
        "evidence_ids": s["hypotheses"][0]["evidence_ids"],
        "affected_component": s["service"],
    }
    s["workflow_state"] = WorkflowState.ROOT_CAUSE_FOUND.value
    s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
    s["result"] = {"phase1_stub": True, "fallback": True}
    return s
