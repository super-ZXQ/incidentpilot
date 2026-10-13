"""Pydantic API / domain schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_serializer

from incidentpilot.models.enums import (
    AgentRunStatus,
    ApprovalDecision,
    Severity,
    WorkflowState,
)


class APIModel(BaseModel):
    @field_serializer("*", mode="wrap")
    def serialize_utc(self, value, handler):
        # SQLite test storage drops tzinfo; persisted timestamps are UTC.
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return handler(value)


class IncidentCreate(APIModel):
    incident_id: str | None = Field(default=None)
    title: str
    service: str
    severity: Severity = Severity.SEV3
    symptom: str
    start_time: datetime | None = None
    repository: str = ""
    environment: str = "reference-production"
    payload: dict[str, Any] = Field(default_factory=dict)


class IncidentOut(APIModel):
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


class RunOut(APIModel):
    id: str
    run_id: str
    incident_pk: str
    incident_id: str = ""
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


class CreateRunAccepted(APIModel):
    incident_id: str
    run_id: str
    status: AgentRunStatus
    workflow_state: str = WorkflowState.INCIDENT_RECEIVED.value


class EvidenceOut(APIModel):
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


class ToolCallOut(APIModel):
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


class PatchArtifactOut(APIModel):
    patch_artifact_id: str
    base_commit_sha: str
    patch_diff: str
    patch_hash: str
    test_run_id: str | None = None
    immutable: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


class ApprovalRequest(APIModel):
    decision: ApprovalDecision
    actor: str | None = None  # Legacy input accepted but never trusted.
    patch_artifact_id: str | None = None
    patch_hash: str | None = None
    reason: str = ""


class ApprovalOut(APIModel):
    id: str
    run_pk: str
    patch_artifact_id: str | None = None
    decision: str | None = None
    actor: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    decided_at: datetime | None = None

    model_config = {"from_attributes": True}


class TestRunOut(APIModel):
    test_run_id: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    mode: str
    patch_hash: str
    base_commit_sha: str
    model_config = {"from_attributes": True}


class HealthOut(APIModel):
    status: str = "ok"
    app: str
    environment: str
