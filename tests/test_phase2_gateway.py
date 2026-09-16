"""Phase 2 tests: Tool Gateway + fake tools + agent workflow."""

from __future__ import annotations

import pytest

from incidentpilot.tools.fake import DeterministicFakeTools, register_fake_readonly_tools
from incidentpilot.tools.gateway import BudgetTracker, ToolGateway, ToolRegistry, ToolSpec


@pytest.mark.asyncio
async def test_tool_gateway_registers_and_calls_fake_tools() -> None:
    registry = ToolRegistry()
    register_fake_readonly_tools(registry)
    gateway = ToolGateway(registry=registry, budget=BudgetTracker(20, 10))

    assert set(registry.names()) >= {
        "read_metrics",
        "read_logs",
        "inspect_git_history",
        "inspect_git_diff",
        "read_source_code",
        "query_database_readonly",
    }

    result = await gateway.call("read_metrics", {"service": "orders-api"})
    assert result.status == "SUCCEEDED"
    assert result.output["p95_latency_ms"] == 2800


@pytest.mark.asyncio
async def test_tool_gateway_denies_unknown_tool() -> None:
    registry = ToolRegistry()
    register_fake_readonly_tools(registry)
    gateway = ToolGateway(registry=registry)
    result = await gateway.call("rm_rf_slash", {})
    assert result.status == "DENIED"
    assert "not registered" in (result.error or "")


@pytest.mark.asyncio
async def test_tool_gateway_budget_exhaustion() -> None:
    registry = ToolRegistry()
    register_fake_readonly_tools(registry)
    gateway = ToolGateway(registry=registry, budget=BudgetTracker(max_tool_calls=1, max_steps=10))
    first = await gateway.call("read_metrics", {"service": "orders-api"})
    second = await gateway.call("read_logs", {"service": "orders-api"})
    assert first.status == "SUCCEEDED"
    assert second.status == "DENIED"
    assert "budget" in (second.error or "").lower()


@pytest.mark.asyncio
async def test_readonly_db_tool_blocks_writes() -> None:
    backend = DeterministicFakeTools()
    with pytest.raises(ValueError, match="forbidden"):
        await backend.query_database_readonly(query="DELETE FROM orders")


@pytest.mark.asyncio
async def test_tool_gateway_input_validation() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="needs_service",
            description="test",
            category="readonly_external",
            handler=lambda service: {"ok": True},
            input_schema={
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        )
    )
    gateway = ToolGateway(registry=registry)
    result = await gateway.call("needs_service", {})
    assert result.status == "FAILED"
    assert "missing required" in (result.error or "")


@pytest.mark.asyncio
async def test_agent_workflow_uses_tool_gateway(client) -> None:
    import asyncio

    payload = {
        "title": "orders latency",
        "service": "orders-api",
        "severity": "SEV2",
        "symptom": "P95 latency 400ms -> 2.8s",
        "repository": "local/orders-api",
        "environment": "reference-production",
    }
    resp = await client.post("/v1/incidents", json=payload)
    assert resp.status_code == 202
    data = resp.json()

    for _ in range(80):
        run = (await client.get(f"/v1/runs/{data['run_id']}")).json()
        if run["status"] not in {"PENDING", "RUNNING"}:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("run did not finish")

    assert run["status"] == "NEEDS_HUMAN_INTERVENTION"
    assert run["root_cause"]["evidence_ids"]

    tools = (await client.get(f"/v1/runs/{data['run_id']}/tool-calls")).json()
    names = {t["tool_name"] for t in tools}
    assert "read_metrics" in names
    assert "read_logs" in names
    assert "inspect_git_history" in names
    assert "read_source_code" in names
    assert len(tools) >= 4

    evidence = (await client.get(f"/v1/runs/{data['run_id']}/evidence")).json()
    assert len(evidence) >= 3
    # every evidence binds to a tool call id
    assert all(e.get("tool_call_id") for e in evidence)
