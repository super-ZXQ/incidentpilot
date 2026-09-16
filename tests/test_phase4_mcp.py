"""Phase 4 tests: MCP adapter contracts and tool boundary."""

from __future__ import annotations

import pytest

from incidentpilot.tools.gateway import BudgetTracker, ToolGateway, ToolRegistry
from incidentpilot.tools.mcp_adapter import register_mcp_readonly_tools

READONLY_TOOLS = {
    "read_metrics",
    "read_logs",
    "inspect_git_history",
    "inspect_git_diff",
    "read_source_code",
    "query_database_readonly",
}


@pytest.mark.asyncio
async def test_mcp_adapter_registers_only_readonly_tools() -> None:
    registry = ToolRegistry()
    register_mcp_readonly_tools(registry)
    names = set(registry.names())
    assert names == READONLY_TOOLS
    # mutation tools must not be present
    assert "modify_code" not in names
    assert "execute_generated_patch" not in names
    assert "create_pull_request" not in names


@pytest.mark.asyncio
async def test_mcp_adapter_offline_tools_work() -> None:
    registry = ToolRegistry()
    register_mcp_readonly_tools(registry)
    gateway = ToolGateway(registry=registry, budget=BudgetTracker(20, 10))

    metrics = await gateway.call("read_metrics", {"service": "orders-api"})
    assert metrics.status == "SUCCEEDED"
    assert "p95_latency_ms" in metrics.output

    logs = await gateway.call("read_logs", {"service": "orders-api"})
    assert logs.status == "SUCCEEDED"

    src = await gateway.call("read_source_code", {"path": "app/services/orders.py"})
    assert src.status == "SUCCEEDED"

    db = await gateway.call("query_database_readonly", {"query": "SELECT 1"})
    assert db.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_mcp_server_module_exposes_tools() -> None:
    import importlib.util
    import sys
    from pathlib import Path

    server_path = Path(__file__).resolve().parents[1] / "ops_mcp" / "ops_readonly" / "server.py"
    spec = importlib.util.spec_from_file_location("ops_readonly_server", server_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ops_readonly_server"] = module
    spec.loader.exec_module(module)

    # FastMCP registers tools on the mcp object
    assert hasattr(module, "mcp")
    tools = getattr(module.mcp, "_tool_manager", None)
    assert tools is not None
    # tool manager has registered tool names
    names = set(getattr(tools, "_tools", {}).keys()) if hasattr(tools, "_tools") else set()
    if not names:
        # fallback: call functions directly
        result = module.query_database_readonly("DROP TABLE x")
        assert "forbidden" in str(result).lower() or "error" in result
        result2 = module.query_database_readonly("SELECT 1")
        assert "rows" in result2
    else:
        assert READONLY_TOOLS.issubset(names) or names == READONLY_TOOLS


@pytest.mark.asyncio
async def test_mcp_readonly_sql_rejects_writes() -> None:
    import importlib.util
    import sys
    from pathlib import Path

    server_path = Path(__file__).resolve().parents[1] / "ops_mcp" / "ops_readonly" / "server.py"
    spec = importlib.util.spec_from_file_location("ops_readonly_server2", server_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ops_readonly_server2"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)

    bad = module.query_database_readonly("DELETE FROM orders")
    assert bad.get("error")
    ok = module.query_database_readonly("SELECT 1 as x")
    assert "rows" in ok
