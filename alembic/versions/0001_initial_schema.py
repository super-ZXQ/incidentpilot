"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-16

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("symptom", sa.Text(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("repository", sa.String(length=512), nullable=False),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_incidents_incident_id", "incidents", ["incident_id"], unique=True)
    op.create_index("ix_incidents_service", "incidents", ["service"], unique=False)

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("incident_pk", sa.String(length=36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("workflow_state", sa.String(length=64), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("root_cause", sa.JSON(), nullable=True),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("patch_attempt_count", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_runs_run_id", "agent_runs", ["run_id"], unique=True)
    op.create_index("ix_agent_runs_incident_pk", "agent_runs", ["incident_pk"], unique=False)

    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("run_pk", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("source", sa.String(length=256), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_evidence_id", "evidence", ["evidence_id"], unique=True)
    op.create_index("ix_evidence_run_pk", "evidence", ["run_pk"], unique=False)

    op.create_table(
        "hypotheses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_pk", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("verification_notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hypotheses_run_pk", "hypotheses", ["run_pk"], unique=False)

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=False),
        sa.Column("run_pk", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tool_calls_tool_call_id", "tool_calls", ["tool_call_id"], unique=True)
    op.create_index("ix_tool_calls_run_pk", "tool_calls", ["run_pk"], unique=False)
    op.create_index("ix_tool_calls_tool_name", "tool_calls", ["tool_name"], unique=False)
    op.create_index("ix_tool_calls_run_name", "tool_calls", ["run_pk", "tool_name"], unique=False)

    op.create_table(
        "patch_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_pk", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("patch_diff", sa.Text(), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_patch_attempts_run_pk", "patch_attempts", ["run_pk"], unique=False)

    op.create_table(
        "patch_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("patch_artifact_id", sa.String(length=64), nullable=False),
        sa.Column("run_pk", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("patch_diff", sa.Text(), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("test_run_id", sa.String(length=64), nullable=True),
        sa.Column("immutable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_patch_artifacts_patch_artifact_id", "patch_artifacts", ["patch_artifact_id"], unique=True
    )
    op.create_index("ix_patch_artifacts_run_pk", "patch_artifacts", ["run_pk"], unique=False)
    op.create_index("ix_patch_artifacts_patch_hash", "patch_artifacts", ["patch_hash"], unique=False)

    op.create_table(
        "test_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("test_run_id", sa.String(length=64), nullable=False),
        sa.Column("run_pk", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("patch_attempt_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=False),
        sa.Column("stderr", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("command", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_test_runs_test_run_id", "test_runs", ["test_run_id"], unique=True)
    op.create_index("ix_test_runs_run_pk", "test_runs", ["run_pk"], unique=False)

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_pk", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("patch_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approvals_run_pk", "approvals", ["run_pk"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_pk", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_run_pk", "audit_events", ["run_pk"], unique=False)
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"], unique=False)


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("approvals")
    op.drop_table("test_runs")
    op.drop_table("patch_artifacts")
    op.drop_table("patch_attempts")
    op.drop_table("tool_calls")
    op.drop_table("hypotheses")
    op.drop_table("evidence")
    op.drop_table("agent_runs")
    op.drop_table("incidents")
