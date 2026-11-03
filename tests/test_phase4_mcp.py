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
    assert src.output["total_lines"] == 4 and src.output["has_more"] is False

    page = await gateway.call("read_source_code",
                              {"path": "app/services/orders.py", "start_line": 3})
    assert page.status == "SUCCEEDED"
    assert page.output["start_line"] == 3
    assert "N+1" not in page.output["content"]

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


def test_source_reads_page_to_the_injected_line(monkeypatch) -> None:
    """The context projection caps strings at 4000 chars, so a 10 KB file was cut
    before the endpoint body. Paging must make the injected loop observable."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1] / "reference" / "orders_api"
    monkeypatch.setenv("REFERENCE_REPO_PATH", str(repo))
    module = _load_server_module("ops_readonly_server_paging")

    pages = []
    start_line = 1
    while True:
        window = module.read_source_code(repository="reference/orders_api",
                                         path="app.py", start_line=start_line)
        assert "error" not in window
        assert "truncated" not in window, "the gateway rejects truncated payloads as incomplete"
        pages.append(window)
        if not window["has_more"]:
            break
        assert window["end_line"] >= window["start_line"]
        start_line = window["end_line"] + 1
        assert len(pages) < 20, "paging failed to terminate"

    source = repo.joinpath("app.py").read_text()
    source_lines = source.splitlines(keepends=True)
    for page in pages:
        assert page["total_lines"] == len(source_lines)
        assert len(page["content"]) <= module.SOURCE_WINDOW_CHARS
        assert page["content"] == "".join(
            source_lines[page["start_line"] - 1:page["end_line"]]
        )
    for previous, following in zip(pages[:-1], pages[1:], strict=True):
        assert following["start_line"] == previous["end_line"] + 1

    assert "".join(page["content"] for page in pages) == source
    assert pages[-1]["end_line"] == pages[-1]["total_lines"]

    injected = [
        page for page in pages
        if "for order in orders" in page["content"] and "per_row_delay" in page["content"]
    ]
    assert injected, "the injected N+1 loop must be reachable through paging"
    loop_line = source.splitlines().index("        for order in orders:") + 1
    assert injected[0]["start_line"] <= loop_line <= injected[0]["end_line"]


def test_source_read_beyond_eof_reports_length_instead_of_failing(monkeypatch) -> None:
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1] / "reference" / "orders_api"
    monkeypatch.setenv("REFERENCE_REPO_PATH", str(repo))
    module = _load_server_module("ops_readonly_server_eof")

    window = module.read_source_code(path="app.py", start_line=100_000)
    assert "error" not in window
    assert window["content"] == ""
    assert window["has_more"] is False
    assert window["total_lines"] == len(repo.joinpath("app.py").read_text().splitlines())
    assert module.read_source_code(path="app.py", start_line=0)["error"]


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
    # `/etc` is rooted but not absolute on Windows, so it is a label there; only a
    # platform-absolute path outside the repository may trigger the rejection.
    outside = tmp_path / "outside"
    with pytest.raises(ValueError, match="outside the configured reference repository"):
        module._resolve_reference_repo(str(outside))

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
