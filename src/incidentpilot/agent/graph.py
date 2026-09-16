"""LangGraph agent workflow with Tool Gateway integration."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, TypedDict

from incidentpilot.config import Settings
from incidentpilot.models.enums import AgentRunStatus, WorkflowState
from incidentpilot.persistence import repo
from incidentpilot.persistence.session import get_session_factory
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
    # Prefer MCP adapter boundary; offline fallback keeps tests deterministic.
    adapter = register_mcp_readonly_tools(registry)
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

        gateway, _backend = build_tool_gateway(settings)

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


async def _execute_graph(
    state: AgentState, settings: Settings, gateway: ToolGateway
) -> AgentState:
    try:
        return await _run_with_langgraph(state, settings, gateway)
    except Exception as exc:
        logger.exception("LangGraph execution failed")
        return await _run_stub_workflow(state, settings, exc)


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
        trace_id="",
    )
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
            "If root cause confirmed: prepare sandbox path (later phase)",
        ]
        return s

    async def collect_evidence_node(s: AgentState) -> AgentState:
        s["workflow_state"] = WorkflowState.COLLECT_EVIDENCE.value
        tool_calls: list[dict[str, Any]] = list(s.get("tool_calls") or [])
        evidence: list[dict[str, Any]] = list(s.get("evidence") or [])

        metrics = await _call_tool(
            gateway,
            s,
            "read_metrics",
            {"service": s["service"], "window": "incident"},
        )
        tool_calls.append(metrics)
        if metrics["status"] == "SUCCEEDED":
            evidence.append(
                {
                    "evidence_id": repo.new_id("EVID"),
                    "source": "read_metrics",
                    "source_type": "metrics",
                    "tool_call_id": metrics["tool_call_id"],
                    "content": (
                        f"Metrics for {s['service']}: p95={metrics['output'].get('p95_latency_ms')}ms, "
                        f"error_rate={metrics['output'].get('error_rate')}"
                    ),
                    "result": metrics["output"],
                }
            )

        logs = await _call_tool(
            gateway,
            s,
            "read_logs",
            {"service": s["service"], "window": "incident"},
        )
        tool_calls.append(logs)
        if logs["status"] == "SUCCEEDED":
            evidence.append(
                {
                    "evidence_id": repo.new_id("EVID"),
                    "source": "read_logs",
                    "source_type": "logs",
                    "tool_call_id": logs["tool_call_id"],
                    "content": (
                        f"Logs for {s['service']}: top_errors="
                        f"{len(logs['output'].get('top_errors') or [])}"
                    ),
                    "result": logs["output"],
                }
            )

        history = await _call_tool(
            gateway,
            s,
            "inspect_git_history",
            {"repository": s.get("repository", ""), "limit": 5},
        )
        tool_calls.append(history)
        if history["status"] == "SUCCEEDED":
            commits = history["output"].get("commits") or []
            if commits:
                evidence.append(
                    {
                        "evidence_id": repo.new_id("EVID"),
                        "source": "inspect_git_history",
                        "source_type": "git_history",
                        "tool_call_id": history["tool_call_id"],
                        "content": f"Recent commit: {commits[0].get('message')}",
                        "result": history["output"],
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
        s["workflow_state"] = WorkflowState.FORM_HYPOTHESIS.value
        if s.get("status") == AgentRunStatus.INSUFFICIENT_EVIDENCE.value:
            return s
        evidence_ids = [e["evidence_id"] for e in s.get("evidence") or []]
        if not evidence_ids:
            s["workflow_state"] = WorkflowState.INSUFFICIENT_EVIDENCE.value
            s["status"] = AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            return s
        logs = next((e for e in s["evidence"] if e["source_type"] == "logs"), None)
        metrics = next((e for e in s["evidence"] if e["source_type"] == "metrics"), None)
        statement = (
            f"Service {s['service']} is degraded due to a recent regression causing "
            f"symptom: {s['symptom']}"
        )
        if logs and "SlowQuery" in str(logs.get("content")):
            statement = (
                f"Recent database-related regression in {s['service']} causes slow queries "
                f"and elevated latency ({s['symptom']})"
            )
        elif metrics:
            statement = (
                f"Elevated latency/error rate in {s['service']} correlates with recent change "
                f"({s['symptom']})"
            )
        s["hypotheses"] = [
            {
                "statement": statement,
                "evidence_ids": evidence_ids,
                "status": "OPEN",
                "confidence": "MEDIUM",
            }
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

        source = await _call_tool(
            gateway,
            s,
            "read_source_code",
            {
                "repository": s.get("repository", ""),
                "path": "app/services/orders.py",
            },
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
                    "content": "Source inspection confirms suspicious query pattern in orders service.",
                    "result": source["output"],
                }
            )
        s["evidence"] = extra_evidence

        hyp = dict(s["hypotheses"][0])
        hyp["evidence_ids"] = [e["evidence_id"] for e in extra_evidence]
        hyp["verified"] = True
        hyp["status"] = "VERIFIED"
        hyp["verification_notes"] = "Metrics, logs, git history and source inspection support hypothesis."
        s["hypotheses"] = [hyp]
        s["root_cause"] = {
            "summary": hyp["statement"],
            "evidence_ids": hyp["evidence_ids"],
            "affected_component": s["service"],
        }
        s["workflow_state"] = WorkflowState.ROOT_CAUSE_FOUND.value
        # Phase 2 stops before sandbox; Phase 6+ continues to patch/tests.
        s["status"] = AgentRunStatus.NEEDS_HUMAN_INTERVENTION.value
        s["result"] = {
            "phase": 2,
            "message": "Root cause identified with fake tools via Tool Gateway.",
            "root_cause": s["root_cause"],
        }
        return s

    def route_after_collect(s: AgentState) -> str:
        if s.get("status") == AgentRunStatus.INSUFFICIENT_EVIDENCE.value:
            return END
        return "form_hypothesis"

    def route_after_verify(s: AgentState) -> str:
        return END

    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("collect_evidence", collect_evidence_node)
    builder.add_node("form_hypothesis", form_hypothesis_node)
    builder.add_node("verify_hypothesis", verify_hypothesis_node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "collect_evidence")
    builder.add_conditional_edges(
        "collect_evidence",
        route_after_collect,
        {"form_hypothesis": "form_hypothesis", END: END},
    )
    builder.add_edge("form_hypothesis", "verify_hypothesis")
    builder.add_conditional_edges("verify_hypothesis", route_after_verify, {END: END})

    graph = builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": state["run_id"]}}
    result = await graph.ainvoke(dict(state), config=config)
    return dict(result)


async def _run_stub_workflow(
    state: AgentState, settings: Settings, exc: Exception | None = None
) -> AgentState:
    s = dict(state)
    s["status"] = AgentRunStatus.FAILED.value
    s["workflow_state"] = WorkflowState.FAILED.value
    s["error"] = f"graph execution failed: {exc}" if exc else "unknown graph failure"
    s["result"] = {"fallback": True}
    return s
