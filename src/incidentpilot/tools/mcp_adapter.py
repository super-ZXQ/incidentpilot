"""MCP client adapter for Tool Gateway (stdio transport)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from incidentpilot.tools.gateway import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

SERVER_PATH = Path(__file__).resolve().parents[3] / "ops_mcp" / "ops_readonly" / "server.py"


class MCPReadonlyAdapter:
    """In-process adapter that can call MCP tools via stdio client when available.

    For unit tests we primarily exercise tool contracts; live MCP stdio is
    used when MCP_LIVE=true and the server module is importable.
    """

    def __init__(self, live: bool | None = None, settings=None) -> None:
        if live is None:
            live = os.environ.get("MCP_LIVE", "false").lower() == "true"
        self.live = live
        self.settings = settings
        self._session = None
        self._client = None

    async def connect(self) -> None:
        if not self.live:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        allowed_env = {
            "PATH",
            "PYTHONPATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "REFERENCE_REPO_PATH",
            "REFERENCE_LOGS_PATH",
            "REFERENCE_ORDERS_API_URL",
            "REFERENCE_DB_URL",
        }
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed_env}
        if self.settings is not None:
            environment.update({"REFERENCE_REPO_PATH": self.settings.reference_repo_path,
                                "REFERENCE_LOGS_PATH": self.settings.reference_logs_path,
                                "REFERENCE_ORDERS_API_URL": self.settings.reference_orders_api_url,
                                "REFERENCE_DB_URL": self.settings.reference_db_url})
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_PATH)],
            env=environment,
        )
        # Store context managers for later use
        self._stdio_cm = stdio_client(params)
        read, write = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

    async def disconnect(self) -> None:
        if self._session is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
                await self._stdio_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("mcp disconnect error", exc_info=True)
            self._session = None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.live and self._session is not None:
            result = await self._session.call_tool(name, arguments)
            if result.isError:
                raise ValueError("MCP tool signaled isError")
            texts = []
            for block in result.content:
                text = getattr(block, "text", None)
                if text:
                    texts.append(text)
            if len(texts) == 1:
                try:
                    import json

                    payload = json.loads(texts[0])
                    if not isinstance(payload, dict) or not payload or payload.get("error"):
                        raise ValueError("MCP returned invalid, empty or error payload")
                    return payload
                except json.JSONDecodeError:
                    raise ValueError("MCP returned malformed JSON") from None
            raise ValueError("MCP must return one complete JSON result")
        if self.live:
            raise RuntimeError("MCP live adapter is not connected; fake fallback is disabled")
        # Explicit deterministic fake backend used only by unit tests.
        return await _offline_tool(name, arguments)


async def _offline_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from incidentpilot.tools.fake import DeterministicFakeTools

    backend = DeterministicFakeTools()
    mapping = {
        "read_metrics": backend.read_metrics,
        "read_logs": backend.read_logs,
        "inspect_git_history": backend.inspect_git_history,
        "inspect_git_diff": backend.inspect_git_diff,
        "read_source_code": backend.read_source_code,
        "query_database_readonly": backend.query_database_readonly,
    }
    handler = mapping.get(name)
    if handler is None:
        return {"error": f"unknown mcp tool: {name}"}
    return await handler(**arguments)


def register_mcp_readonly_tools(registry: ToolRegistry, adapter: MCPReadonlyAdapter | None = None) -> MCPReadonlyAdapter:
    adapter = adapter or MCPReadonlyAdapter(live=False)

    async def _call(name: str, **kwargs: Any) -> dict[str, Any]:
        return await adapter.call_tool(name, kwargs)

    specs = [
        ToolSpec(
            name="read_metrics",
            description="Read metrics via Ops Readonly MCP",
            category="readonly_external",
            handler=lambda service="orders-api", window="incident": _call(
                "read_metrics", service=service, window=window
            ),
            input_schema={
                "type": "object",
                "properties": {"service": {"type": "string"}, "window": {"type": "string"}},
                "required": ["service"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="read_logs",
            description="Read logs via Ops Readonly MCP",
            category="readonly_external",
            handler=lambda service="orders-api", window="incident": _call(
                "read_logs", service=service, window=window
            ),
            input_schema={
                "type": "object",
                "properties": {"service": {"type": "string"}, "window": {"type": "string"}},
                "required": ["service"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="inspect_git_history",
            description="Inspect git history via MCP",
            category="readonly_external",
            handler=lambda repository="", limit=10: _call(
                "inspect_git_history", repository=repository, limit=limit
            ),
            input_schema={
                "type": "object",
                "properties": {"repository": {"type": "string"}, "limit": {"type": "integer"}},
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="inspect_git_diff",
            description="Inspect git diff via MCP",
            category="readonly_external",
            handler=lambda repository="", commit_sha="HEAD": _call(
                "inspect_git_diff", repository=repository, commit_sha=commit_sha
            ),
            input_schema={
                "type": "object",
                "properties": {"repository": {"type": "string"}, "commit_sha": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="read_source_code",
            description="Read source code via MCP",
            category="readonly_external",
            handler=lambda path="app.py", repository="": _call(
                "read_source_code", path=path, repository=repository
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "repository": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="query_database_readonly",
            description="Read-only SQL via MCP",
            category="readonly_external",
            handler=lambda query="SELECT 1": _call("query_database_readonly", query=query),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    ]
    for spec in specs:
        registry.register(spec)
    return adapter
