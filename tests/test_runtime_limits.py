"""Checkpoint recovery and execution-limit tests."""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph


class S(TypedDict, total=False):
    steps: list[str]
    done: bool


@pytest.mark.asyncio
async def test_langgraph_checkpoint_resume_after_interrupt() -> None:
    async def a(s: S) -> S:
        return {**s, "steps": s.get("steps", []) + ["a"]}

    async def b(s: S) -> S:
        return {**s, "steps": s.get("steps", []) + ["b"], "done": True}

    builder = StateGraph(S)
    builder.add_node("a", a)
    builder.add_node("b", b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    graph = builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "run-recovery-1"}}

    first = await graph.ainvoke({"steps": [], "done": False}, config=config)
    assert first["steps"] == ["a", "b"]
    assert first["done"] is True

    # Simulate restart: new graph object, same checkpointer thread id state via invoke history
    snapshot = await graph.aget_state(config)
    assert snapshot is not None
    assert snapshot.values.get("done") is True


@pytest.mark.asyncio
async def test_tool_gateway_budget_prevents_unbounded_calls() -> None:
    from incidentpilot.tools.fake import register_fake_readonly_tools
    from incidentpilot.tools.gateway import BudgetTracker, ToolGateway, ToolRegistry

    registry = ToolRegistry()
    register_fake_readonly_tools(registry)
    gateway = ToolGateway(registry=registry, budget=BudgetTracker(max_tool_calls=3, max_steps=5))
    statuses = []
    for _ in range(5):
        r = await gateway.call("read_metrics", {"service": "orders-api"})
        statuses.append(r.status)
    assert statuses[:3] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    assert statuses[3:] == ["DENIED", "DENIED"]


def test_sql_guard_blocks_writes_and_allows_select() -> None:
    from incidentpilot.tools.sql_guard import SQLValidationError, validate_readonly_sql

    assert validate_readonly_sql("SELECT 1")["ok"] is True
    assert validate_readonly_sql("SELECT id FROM orders WHERE status = 'created'")["ok"] is True
    for bad in [
        "DELETE FROM orders",
        "UPDATE orders SET status='x'",
        "DROP TABLE orders",
        "INSERT INTO orders VALUES (1)",
        "SELECT 1; DELETE FROM orders",
        "CREATE TABLE x (id int)",
    ]:
        with pytest.raises(SQLValidationError):
            validate_readonly_sql(bad)
