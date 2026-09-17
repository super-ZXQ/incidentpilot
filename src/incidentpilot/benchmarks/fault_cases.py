"""Fault case loading (package-level)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

REGISTERED_FAULT_TYPES = {
    "slow_database_query",
    "missing_index",
    "n_plus_one_query",
    "bad_code_commit",
    "null_exception",
    "dependency_timeout",
    "connection_pool_exhaustion",
    "schema_mismatch",
    "cache_failure",
    "incorrect_configuration",
    "bad_query_refactor",
}


class FaultGroundTruth(BaseModel):
    fault_category: str
    affected_component: str
    root_cause: str
    causal_facts: list[str] = Field(min_length=1)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class FaultCase(BaseModel):
    id: str
    title: str
    category: str
    fault_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    incident: dict[str, Any] = Field(default_factory=dict)
    ground_truth: FaultGroundTruth
    expected_evidence_sources: list[str] = Field(default_factory=list)
    expected_fix_behavior: dict[str, Any] = Field(default_factory=dict)

    def validate_registered(self) -> None:
        if self.fault_type not in REGISTERED_FAULT_TYPES:
            raise ValueError(f"fault_type '{self.fault_type}' is not registered")


def load_fault_case(path: str | Path) -> FaultCase:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    fault_type = data.get("fault_type") if isinstance(data, dict) else None
    if fault_type not in REGISTERED_FAULT_TYPES:
        raise ValueError(f"fault_type '{fault_type}' is not registered")
    case = FaultCase.model_validate(data)
    case.validate_registered()
    return case


def load_fault_cases(directory: str | Path) -> list[FaultCase]:
    directory = Path(directory)
    return [load_fault_case(p) for p in sorted(directory.glob("*.yaml"))]
