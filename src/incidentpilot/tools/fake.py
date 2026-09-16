"""Deterministic fake tools for Phase 2 development and tests."""

from __future__ import annotations

from typing import Any

from incidentpilot.tools.gateway import ToolRegistry, ToolSpec


class DeterministicFakeTools:
    """In-process fake tool backend used until MCP / Reference Environment land."""

    def __init__(self, fault_hint: str = "slow_database_query") -> None:
        self.fault_hint = fault_hint

    async def read_metrics(
        self, service: str = "orders-api", window: str = "incident", **_: Any
    ) -> dict[str, Any]:
        return {
            "service": service,
            "window": window,
            "p95_latency_ms": 2800,
            "error_rate": 0.07,
            "signal": "elevated_latency",
            "fault_hint": self.fault_hint,
        }

    async def read_logs(
        self, service: str = "orders-api", window: str = "incident", **_: Any
    ) -> dict[str, Any]:
        return {
            "service": service,
            "window": window,
            "error_count": 42,
            "top_errors": [
                {"class": "SlowQueryError", "message": "query exceeded 2000ms", "count": 30},
                {"class": "TimeoutError", "message": "db operation timed out", "count": 12},
            ],
            "fault_hint": self.fault_hint,
        }

    async def inspect_git_history(
        self, repository: str = "", limit: int = 10, **_: Any
    ) -> dict[str, Any]:
        return {
            "repository": repository,
            "commits": [
                {
                    "sha": "abc1234",
                    "message": "Add orders listing endpoint without pagination filter",
                    "author": "dev",
                    "date": "2026-09-15T10:00:00Z",
                }
            ],
            "fault_hint": self.fault_hint,
        }

    async def inspect_git_diff(
        self, repository: str = "", commit_sha: str = "abc1234", **_: Any
    ) -> dict[str, Any]:
        return {
            "repository": repository,
            "commit_sha": commit_sha,
            "files": ["app/services/orders.py"],
            "diff": "- query = select(Order)\n+ query = select(Order).join(Order.items)",
            "fault_hint": self.fault_hint,
        }

    async def read_source_code(
        self, repository: str = "", path: str = "app/services/orders.py", **_: Any
    ) -> dict[str, Any]:
        return {
            "repository": repository,
            "path": path,
            "content": (
                "def list_orders(db):\n"
                "    # N+1: items loaded per order\n"
                "    orders = db.query(Order).all()\n"
                "    return [o.with_items() for o in orders]\n"
            ),
            "fault_hint": self.fault_hint,
        }

    async def query_database_readonly(
        self, query: str = "SELECT 1", **_: Any
    ) -> dict[str, Any]:
        forbidden = ("insert", "update", "delete", "drop", "alter", "truncate", "create")
        lowered = query.lower()
        if any(kw in lowered for kw in forbidden):
            raise ValueError("write SQL is forbidden in query_database_readonly")
        return {
            "query": query,
            "rows": [{"slow_queries": 12, "max_duration_ms": 2400}],
            "fault_hint": self.fault_hint,
        }


def register_fake_readonly_tools(
    registry: ToolRegistry,
    backend: DeterministicFakeTools | None = None,
) -> DeterministicFakeTools:
    backend = backend or DeterministicFakeTools()

    specs = [
        ToolSpec(
            name="read_metrics",
            description="Read service metrics from reference environment",
            category="readonly_external",
            handler=backend.read_metrics,
            input_schema={
                "type": "object",
                "properties": {"service": {"type": "string"}, "window": {"type": "string"}},
                "required": ["service"],
            },
        ),
        ToolSpec(
            name="read_logs",
            description="Read structured logs from reference environment",
            category="readonly_external",
            handler=backend.read_logs,
            input_schema={
                "type": "object",
                "properties": {"service": {"type": "string"}, "window": {"type": "string"}},
                "required": ["service"],
            },
        ),
        ToolSpec(
            name="inspect_git_history",
            description="Inspect recent git history",
            category="readonly_external",
            handler=backend.inspect_git_history,
            input_schema={
                "type": "object",
                "properties": {"repository": {"type": "string"}, "limit": {"type": "integer"}},
            },
        ),
        ToolSpec(
            name="inspect_git_diff",
            description="Inspect a commit diff",
            category="readonly_external",
            handler=backend.inspect_git_diff,
            input_schema={
                "type": "object",
                "properties": {
                    "repository": {"type": "string"},
                    "commit_sha": {"type": "string"},
                },
            },
        ),
        ToolSpec(
            name="read_source_code",
            description="Read application source file",
            category="readonly_external",
            handler=backend.read_source_code,
            input_schema={
                "type": "object",
                "properties": {
                    "repository": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        ),
        ToolSpec(
            name="query_database_readonly",
            description="Run a read-only SQL query",
            category="readonly_external",
            handler=backend.query_database_readonly,
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    ]
    for spec in specs:
        registry.register(spec)
    return backend
