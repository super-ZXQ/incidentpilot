"""Tool Gateway: unified governance layer for all Agent tools.

Agent never calls systems directly. All tools route through this layer.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from incidentpilot.models.enums import ToolCallStatus

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    name: str
    description: str
    category: str  # readonly_external | sandboxed_mutation | approval
    handler: Callable[..., Any]
    timeout_seconds: float = 30.0
    max_retries: int = 1
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    tool_call_id: str
    tool_name: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0
    input: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "input": self.input,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())


class BudgetTracker:
    def __init__(self, max_tool_calls: int, max_steps: int) -> None:
        self.max_tool_calls = max_tool_calls
        self.max_steps = max_steps
        self.tool_call_count = 0
        self.step_count = 0

    def can_call_tool(self) -> bool:
        return self.tool_call_count < self.max_tool_calls

    def can_step(self) -> bool:
        return self.step_count < self.max_steps

    def record_tool_call(self) -> None:
        self.tool_call_count += 1

    def record_step(self) -> None:
        self.step_count += 1


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        budget: BudgetTracker | None = None,
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.registry = registry
        self.budget = budget or BudgetTracker(max_tool_calls=100, max_steps=50)
        self.audit_sink = audit_sink
        self.audit_log: list[dict[str, Any]] = []

    def _validate_input(self, spec: ToolSpec, payload: dict[str, Any]) -> dict[str, Any]:
        schema = spec.input_schema or {}
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValueError(f"missing required fields for {spec.name}: {missing}")
        cleaned: dict[str, Any] = {}
        if properties:
            for key, value in payload.items():
                if key in properties:
                    cleaned[key] = value
                else:
                    # Keep extra fields for flexible tools, but schema-first tools can reject
                    cleaned[key] = value
        else:
            cleaned = dict(payload)
        return cleaned

    def _audit(self, event: dict[str, Any]) -> None:
        self.audit_log.append(event)
        if self.audit_sink is not None:
            self.audit_sink(event)

    async def call(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        run_id: str = "",
        trace_id: str = "",
    ) -> ToolResult:
        tool_call_id = f"TC-{uuid.uuid4().hex[:12]}"
        spec = self.registry.get(name)
        if spec is None:
            result = ToolResult(
                tool_call_id=tool_call_id,
                tool_name=name,
                status=ToolCallStatus.DENIED.value,
                error=f"tool not registered: {name}",
                input=payload,
            )
            self._audit(
                {
                    "event": "tool_denied",
                    "tool_call_id": tool_call_id,
                    "tool_name": name,
                    "run_id": run_id,
                    "trace_id": trace_id,
                }
            )
            return result

        if not self.budget.can_call_tool():
            result = ToolResult(
                tool_call_id=tool_call_id,
                tool_name=name,
                status=ToolCallStatus.DENIED.value,
                error="tool call budget exhausted",
                input=payload,
            )
            self._audit(
                {
                    "event": "budget_exhausted",
                    "tool_call_id": tool_call_id,
                    "tool_name": name,
                    "run_id": run_id,
                }
            )
            return result

        try:
            cleaned = self._validate_input(spec, payload)
        except ValueError as exc:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=name,
                status=ToolCallStatus.FAILED.value,
                error=str(exc),
                input=payload,
            )

        self.budget.record_tool_call()
        started = time.perf_counter()
        last_error: str | None = None

        for attempt in range(spec.max_retries + 1):
            try:
                output = await asyncio.wait_for(
                    _maybe_await(spec.handler(**cleaned)),
                    timeout=spec.timeout_seconds,
                )
                latency = (time.perf_counter() - started) * 1000
                if not isinstance(output, dict):
                    output = {"result": output}
                result = ToolResult(
                    tool_call_id=tool_call_id,
                    tool_name=name,
                    status=ToolCallStatus.SUCCEEDED.value,
                    output=output,
                    latency_ms=latency,
                    input=cleaned,
                )
                self._audit(
                    {
                        "event": "tool_succeeded",
                        "tool_call_id": tool_call_id,
                        "tool_name": name,
                        "run_id": run_id,
                        "trace_id": trace_id,
                        "attempt": attempt,
                        "latency_ms": latency,
                    }
                )
                return result
            except TimeoutError:
                last_error = f"timeout after {spec.timeout_seconds}s"
                status = ToolCallStatus.TIMEOUT.value
            except Exception as exc:
                last_error = str(exc)
                status = ToolCallStatus.FAILED.value
                logger.debug("tool %s attempt %s failed: %s", name, attempt, exc)

        latency = (time.perf_counter() - started) * 1000
        result = ToolResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status=status,
            error=last_error,
            latency_ms=latency,
            input=cleaned,
        )
        self._audit(
            {
                "event": "tool_failed",
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "run_id": run_id,
                "error": last_error,
                "latency_ms": latency,
            }
        )
        return result


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


class FakeToolBackend(ABC):
    """Optional backend interface for fake/local tool implementations."""

    @abstractmethod
    async def read_metrics(self, **kwargs: Any) -> dict[str, Any]: ...

    @abstractmethod
    async def read_logs(self, **kwargs: Any) -> dict[str, Any]: ...

    @abstractmethod
    async def inspect_git_history(self, **kwargs: Any) -> dict[str, Any]: ...

    @abstractmethod
    async def inspect_git_diff(self, **kwargs: Any) -> dict[str, Any]: ...

    @abstractmethod
    async def read_source_code(self, **kwargs: Any) -> dict[str, Any]: ...

    @abstractmethod
    async def query_database_readonly(self, **kwargs: Any) -> dict[str, Any]: ...
