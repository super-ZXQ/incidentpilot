"""SQLAlchemy ORM models for the IncidentPilot control plane."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from incidentpilot.models.enums import (
    AgentRunStatus,
    ApprovalDecision,
    EvidenceSourceType,
    IncidentStatus,
    PatchAttemptStatus,
    Severity,
    TestRunStatus,
    ToolCallStatus,
    WorkflowState,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(256))
    service: Mapped[str] = mapped_column(String(128), index=True)
    severity: Mapped[str] = mapped_column(String(16), default=Severity.SEV3.value)
    symptom: Mapped[str] = mapped_column(Text)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    repository: Mapped[str] = mapped_column(String(512), default="")
    environment: Mapped[str] = mapped_column(String(64), default="reference-production")
    status: Mapped[str] = mapped_column(String(64), default=IncidentStatus.RECEIVED.value)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    runs: Mapped[list[AgentRun]] = relationship(back_populates="incident")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_pk: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    status: Mapped[str] = mapped_column(String(64), default=AgentRunStatus.PENDING.value)
    workflow_state: Mapped[str] = mapped_column(
        String(64), default=WorkflowState.INCIDENT_RECEIVED.value
    )
    plan: Mapped[list[str]] = mapped_column(JSON, default=list)
    root_cause: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_call_count: Mapped[int] = mapped_column(default=0)
    patch_attempt_count: Mapped[int] = mapped_column(default=0)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    incident: Mapped[Incident] = relationship(back_populates="runs")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="run")
    hypotheses: Mapped[list[Hypothesis]] = relationship(back_populates="run")
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="run")
    patch_artifacts: Mapped[list[PatchArtifact]] = relationship(back_populates="run")


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    evidence_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    run_pk: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    source: Mapped[str] = mapped_column(String(256))
    source_type: Mapped[str] = mapped_column(String(64), default=EvidenceSourceType.OTHER.value)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[AgentRun] = relationship(back_populates="evidence")


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_pk: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    statement: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    confidence: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    verified: Mapped[bool] = mapped_column(default=False)
    verification_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    run: Mapped[AgentRun] = relationship(back_populates="hypotheses")


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tool_call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    run_pk: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=ToolCallStatus.PENDING.value)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[AgentRun] = relationship(back_populates="tool_calls")

    __table_args__ = (Index("ix_tool_calls_run_name", "run_pk", "tool_name"),)


class PatchAttempt(Base):
    __tablename__ = "patch_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_pk: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(default=1)
    patch_diff: Mapped[str] = mapped_column(Text, default="")
    patch_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default=PatchAttemptStatus.CREATED.value)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PatchArtifact(Base):
    __tablename__ = "patch_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    patch_artifact_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    run_pk: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    patch_diff: Mapped[str] = mapped_column(Text)
    patch_hash: Mapped[str] = mapped_column(String(64), index=True)
    test_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    immutable: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[AgentRun] = relationship(back_populates="patch_artifacts")


class TestRun(Base):
    __tablename__ = "test_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    test_run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    run_pk: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    patch_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=TestRunStatus.PENDING.value)
    exit_code: Mapped[int | None] = mapped_column(nullable=True)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[float | None] = mapped_column(nullable=True)
    command: Mapped[str] = mapped_column(String(512), default="pytest")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_pk: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    patch_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actor: Mapped[str] = mapped_column(String(128), default="human")
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def is_approved(self) -> bool:
        return self.decision == ApprovalDecision.APPROVE.value


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_pk: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
