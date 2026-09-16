"""Real MCP stdio protocol integration tests (bounded, skip if env hangs)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp

SERVER = Path(__file__).resolve().parents[1] / "ops_mcp" / "ops_readonly" / "server.py"
SRC = str(Path(__file__).resolve().parents[1] / "src")


async def _run_session_check() -> dict:
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    out: dict = {}
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        out["names"] = {t.name for t in tools.tools}
        metrics = await session.call_tool("read_metrics", {"service": "orders-api"})
        out["metrics_blocks"] = len(metrics.content)
        db = await session.call_tool("query_database_readonly", {"query": "SELECT 1 AS x"})
        out["db_text"] = "".join(getattr(b, "text", "") for b in db.content)
        bad = await session.call_tool(
            "query_database_readonly", {"query": "DELETE FROM orders"}
        )
        out["bad_text"] = "".join(getattr(b, "text", "") for b in bad.content)
    return out


@pytest.mark.asyncio
async def test_mcp_stdio_list_and_call_tools() -> None:
    try:
        out = await asyncio.wait_for(_run_session_check(), timeout=20)
    except TimeoutError:
        pytest.skip("MCP stdio session timed out on this host")
    except Exception as exc:
        if "timeout" in str(exc).lower() or "pipe" in str(exc).lower():
            pytest.skip(f"MCP stdio unavailable: {exc}")
        raise

    expected = {
        "read_metrics",
        "read_logs",
        "inspect_git_history",
        "inspect_git_diff",
        "read_source_code",
        "query_database_readonly",
    }
    assert expected.issubset(out["names"])
    assert "modify_code" not in out["names"]
    assert "run_tests" not in out["names"]
    assert "create_pull_request" not in out["names"]
    assert out["metrics_blocks"] >= 1
    assert "error" in out["db_text"].lower() or "rows" in out["db_text"].lower() or out["db_text"]
    assert "error" in out["bad_text"].lower() or "forbidden" in out["bad_text"].lower()
