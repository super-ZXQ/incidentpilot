"""Fault injector: registered handlers only (no arbitrary shell)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# In-process fault state consumed by orders-api
FAULT_STATE: dict[str, Any] = {"fault_type": None, "params": {}, "case_id": None}


def reset_fault() -> None:
    FAULT_STATE["fault_type"] = None
    FAULT_STATE["params"] = {}
    FAULT_STATE["case_id"] = None


def get_fault() -> dict[str, Any]:
    return dict(FAULT_STATE)


class FaultHandler:
    def __init__(self, name: str, setup: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.name = name
        self.setup = setup


HANDLERS: dict[str, FaultHandler] = {}


def register(name: str):
    def deco(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable:
        HANDLERS[name] = FaultHandler(name, fn)
        return fn

    return deco


@register("n_plus_one_query")
def _n_plus_one(params: dict[str, Any]) -> dict[str, Any]:
    return {"per_row_delay": float(params.get("per_row_delay", 0.05))}


@register("slow_database_query")
def _slow_query(params: dict[str, Any]) -> dict[str, Any]:
    return {"delay_seconds": float(params.get("delay_seconds", 0.5))}


@register("null_exception")
def _null_exc(params: dict[str, Any]) -> dict[str, Any]:
    return {"error_message": params.get("error_message", "null pointer in order serializer")}


@register("missing_index")
def _missing_index(params: dict[str, Any]) -> dict[str, Any]:
    # Simulates sequential scan by forcing per-row delay on listing
    return {"per_row_delay": float(params.get("per_row_delay", 0.03))}


@register("dependency_timeout")
def _dep_timeout(params: dict[str, Any]) -> dict[str, Any]:
    return {"delay_seconds": float(params.get("delay_seconds", 1.2))}


@register("schema_mismatch")
def _schema_mismatch(params: dict[str, Any]) -> dict[str, Any]:
    return {"missing_field": params.get("missing_field", "total_cents")}


@register("incorrect_configuration")
def _bad_config(params: dict[str, Any]) -> dict[str, Any]:
    return {"max_limit": int(params.get("max_limit", 1))}


@register("bad_query_refactor")
def _bad_refactor(params: dict[str, Any]) -> dict[str, Any]:
    return {"per_row_delay": float(params.get("per_row_delay", 0.04))}


@register("connection_pool_exhaustion")
def _pool(params: dict[str, Any]) -> dict[str, Any]:
    return {"delay_seconds": float(params.get("delay_seconds", 0.8))}


@register("cache_failure")
def _cache(params: dict[str, Any]) -> dict[str, Any]:
    return {"per_row_delay": float(params.get("per_row_delay", 0.02))}


@register("bad_code_commit")
def _bad_commit(params: dict[str, Any]) -> dict[str, Any]:
    return {"error_message": params.get("error_message", "regression in serializer")}


def apply_fault(fault_type: str, params: dict[str, Any] | None = None, case_id: str | None = None) -> dict[str, Any]:
    if fault_type not in HANDLERS:
        raise ValueError(f"unknown/unregistered fault_type: {fault_type}")
    resolved = HANDLERS[fault_type].setup(params or {})
    FAULT_STATE["fault_type"] = fault_type
    FAULT_STATE["params"] = resolved
    FAULT_STATE["case_id"] = case_id
    return {"fault_type": fault_type, "params": resolved, "case_id": case_id}
