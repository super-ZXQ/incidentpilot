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


def _load_server_module(name: str):
    import importlib.util
    import sys
    from pathlib import Path

    server_path = Path(__file__).resolve().parents[1] / "ops_mcp" / "ops_readonly" / "server.py"
    spec = importlib.util.spec_from_file_location(name, server_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_repository_names_the_configured_repo_not_the_worker_cwd(
    monkeypatch, tmp_path
) -> None:
    """The reference repository is configured once; `repository` is a label.

    The worker runs from the image's /app, the MCP server inherits that CWD, and the
    incident says `reference/orders_api`. Resolving the label against the CWD made the
    same incident work from a checkout and fail in a container.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is required to build the repository fixture")
    repo = tmp_path / "reference" / "orders_api"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "-A"),
        ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "baseline"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    monkeypatch.setenv("REFERENCE_REPO_PATH", str(repo))
    module = _load_server_module("ops_readonly_server_cwd")
    configured = repo.resolve()

    assert module._resolve_reference_repo("") == configured
    assert module._resolve_reference_repo("reference/orders_api") == configured
    assert module._resolve_reference_repo(str(configured)) == configured
    # A relative string never becomes a path the caller controls.
    assert module._resolve_reference_repo("../../etc") == configured
    with pytest.raises(ValueError, match="outside the configured reference repository"):
        module._resolve_reference_repo("/etc")

    history = module.inspect_git_history("reference/orders_api")
    assert "error" not in history
    assert [c["message"] for c in history["commits"]] == ["baseline"]


@pytest.mark.asyncio
async def test_failed_mcp_payload_keeps_the_server_reason() -> None:
    """The reason survives, so a stored ToolCall is still diagnosable."""
    import json
    from types import SimpleNamespace

    from incidentpilot.tools.mcp_adapter import MCPReadonlyAdapter

    class _Session:
        def __init__(self, text: str) -> None:
            self._text = text

        async def call_tool(self, _name, _arguments):
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text=self._text)])

    reason = "fatal: not a git repository (or any parent up to mount point /reference)"
    adapter = MCPReadonlyAdapter(live=True)
    adapter._session = _Session(json.dumps({"repository": "/reference/orders_api",
                                            "commits": [], "error": reason}))
    with pytest.raises(ValueError, match="not a git repository"):
        await adapter.call_tool("inspect_git_history", {"repository": "reference/orders_api"})

    adapter._session = _Session(json.dumps({}))
    with pytest.raises(ValueError, match="empty payload"):
        await adapter.call_tool("read_logs", {})

    adapter._session = _Session(json.dumps([1, 2]))
    with pytest.raises(ValueError, match="non-object payload"):
        await adapter.call_tool("read_logs", {})
