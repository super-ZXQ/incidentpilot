"""Pydantic API / domain schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from incidentpilot.models.enums import (
    AgentRunStatus,
    ApprovalDecision,
    Severity,
    WorkflowState,
)


class IncidentCreate(BaseModel):
    incident_id: str | None = Field(default=None)
    title: str
    service: str
    severity: Severity = Severity.SEV3
    symptom: str
    start_time: datetime | None = None
    repository: str = ""
    environment: str = "reference-production"
    payload: dict[str, Any] = Field(default_factory=dict)


class IncidentOut(BaseModel):
    id: str
    incident_id: str
    title: str
    service: str
    severity: str
    symptom: str
    start_time: datetime
    repository: str
    environment: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RunOut(BaseModel):
    id: str
    run_id: str
    incident_pk: str
    status: str
    workflow_state: str
    plan: list[str] = Field(default_factory=list)
    root_cause: dict[str, Any] | None = None
    tool_call_count: int = 0
    patch_attempt_count: int = 0
    trace_id: str = ""
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None
    updated_at: datetime

    model_config = {"from_attributes": True}


class CreateRunAccepted(BaseModel):
    incident_id: str
    run_id: str
    status: AgentRunStatus
    workflow_state: str = WorkflowState.INCIDENT_RECEIVED.value


class EvidenceOut(BaseModel):
    evidence_id: str
    source: str
    source_type: str
    timestamp: datetime
    tool_call_id: str | None = None
    content: str
    summary: str = ""
    content_hash: str = ""
    result: dict[str, Any] = Field(default_factory=dict)

    model_config = {"from_attributes": True}


class ToolCallOut(BaseModel):
    tool_call_id: str
    tool_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    status: str
    error: str | None = None
    latency_ms: float | None = None
    attempt_count: int = 1
    error_category: str | None = None
    trace_id: str = ""
    created_at: datetime

    model_config = {"from_attributes": True}


class PatchArtifactOut(BaseModel):
    patch_artifact_id: str
    base_commit_sha: str
    patch_diff: str
    patch_hash: str
    test_run_id: str | None = None
    immutable: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


class ApprovalRequest(BaseModel):
    decision: ApprovalDecision
    actor: str = "human"
    reason: str = ""


class ApprovalOut(BaseModel):
    id: str
    run_pk: str
    patch_artifact_id: str | None = None
    decision: str | None = None
    actor: str
    reason: str
    created_at: datetime
    decided_at: datetime | None = None

    model_config = {"from_attributes": True}


class HealthOut(BaseModel):
    status: str = "ok"
    app: str
    environment: str
