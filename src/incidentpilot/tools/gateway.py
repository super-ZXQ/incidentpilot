"""Tool Gateway: unified governance layer for all Agent tools.

Agent never calls systems directly. All tools route through this layer.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

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
    attempt_count: int = 1
    error_category: str | None = None
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "input": self.input,
            "attempt_count": self.attempt_count,
            "error_category": self.error_category,
            "trace_id": self.trace_id,
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
        sleep: Callable[[float], Any] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.registry = registry
        self.budget = budget or BudgetTracker(max_tool_calls=100, max_steps=50)
        self.audit_sink = audit_sink
        self.sleep = sleep
        self.jitter = jitter
        self.audit_log: list[dict[str, Any]] = []

    def _validate_input(self, spec: ToolSpec, payload: dict[str, Any]) -> dict[str, Any]:
        schema = spec.input_schema or {}
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValueError(f"missing required fields for {spec.name}: {missing}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(payload) - set(properties))
            if extras:
                raise ValueError(f"unexpected fields for {spec.name}: {extras}")
        cleaned: dict[str, Any] = {}
        if properties:
            for key, value in payload.items():
                if key in properties:
                    expected = properties[key].get("type")
                    py_type = {"string": str, "integer": int, "number": (int, float), "boolean": bool}.get(expected)
                    if py_type is not None and not isinstance(value, py_type):
                        raise ValueError(f"invalid type for {spec.name}.{key}: expected {expected}")
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
                error_category="permission",
                trace_id=trace_id,
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
                error_category="budget",
                trace_id=trace_id,
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
                error_category="validation",
                trace_id=trace_id,
            )

        self.budget.record_tool_call()
        started = time.perf_counter()
        last_error: str | None = None

        final_category: str | None = None
        attempts = spec.max_retries + 1
        for attempt in range(attempts):
            try:
                from incidentpilot.observability.otel import start_span

                with start_span(
                    "tool_gateway.call",
                    {
                        "run_id": run_id,
                        "trace_id": trace_id,
                        "tool.name": name,
                        "tool.category": spec.category,
                        "tool.attempt": attempt,
                    },
                ):
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
                    attempt_count=attempt + 1,
                    trace_id=trace_id,
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
                final_category = "timeout"
            except Exception as exc:
                last_error = str(exc)
                status = ToolCallStatus.FAILED.value
                final_category, retryable = classify_tool_error(exc)
                logger.debug("tool %s attempt %s failed: %s", name, attempt, exc)

            retryable = final_category in {"timeout", "connection", "rate_limit", "server"}
            latency = (time.perf_counter() - started) * 1000
            self._audit(
                {
                    "event": "tool_attempt_failed",
                    "tool_call_id": tool_call_id,
                    "tool_name": name,
                    "run_id": run_id,
                    "trace_id": trace_id,
                    "attempt": attempt + 1,
                    "error_category": final_category,
                    "latency_ms": latency,
                }
            )
            if not retryable or attempt + 1 >= attempts:
                break
            delay = min(5.0, 0.25 * (2**attempt)) * (0.5 + self.jitter())
            await _maybe_await(self.sleep(delay))

        latency = (time.perf_counter() - started) * 1000
        result = ToolResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status=status,
            error=last_error,
            latency_ms=latency,
            input=cleaned,
            attempt_count=attempt + 1,
            error_category=final_category,
            trace_id=trace_id,
        )
        self._audit(
            {
                "event": "tool_failed",
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "run_id": run_id,
                "error": last_error,
                "error_category": final_category,
                "attempt": attempt + 1,
                "trace_id": trace_id,
                "latency_ms": latency,
            }
        )
        from incidentpilot.observability.metrics import TOOL_FAILURES

        TOOL_FAILURES.labels(tool=name, error_category=final_category or "unknown").inc()
        return result


def classify_tool_error(exc: Exception) -> tuple[str, bool]:
    """Map errors to stable categories and whether retry is safe."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout", True
    if isinstance(exc, (ConnectionError, ConnectionResetError, httpx.NetworkError)):
        return "connection", True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return "rate_limit", True
        if code in {500, 502, 503, 504}:
            return "server", True
        return "http_permanent", False
    if isinstance(exc, PermissionError):
        return "permission", False
    if isinstance(exc, (TypeError, ValueError)):
        return "validation", False
    return "permanent", False


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
