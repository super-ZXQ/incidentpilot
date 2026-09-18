"""add worker leases and durable side effects

Revision ID: 0004_worker_leases
Revises: 0003_approval_idempotency
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_worker_leases"
down_revision: str | None = "0003_approval_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("worker_id", sa.String(128), nullable=True))
    op.add_column("agent_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_runs", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("agent_runs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_runs", sa.Column("failure_category", sa.String(64), nullable=True))
    op.create_index("ix_agent_runs_worker_id", "agent_runs", ["worker_id"])
    op.create_index("ix_agent_runs_lease_expires_at", "agent_runs", ["lease_expires_at"])
    op.create_index("ix_agent_runs_next_attempt_at", "agent_runs", ["next_attempt_at"])
    op.create_index(
        "ix_agent_runs_claimable",
        "agent_runs",
        ["status", "next_attempt_at", "lease_expires_at"],
    )

    op.add_column("tool_calls", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("tool_calls", sa.Column("error_category", sa.String(64), nullable=True))
    op.add_column("tool_calls", sa.Column("trace_id", sa.String(64), nullable=False, server_default=""))

    op.create_table(
        "external_side_effects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_pk", sa.String(36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("effect_type", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_pk", "effect_type", name="ux_side_effect_run_type"),
    )
    op.create_index("ix_external_side_effects_run_pk", "external_side_effects", ["run_pk"])
    op.create_index(
        "ix_external_side_effects_idempotency_key",
        "external_side_effects",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("external_side_effects")
    op.drop_column("tool_calls", "trace_id")
    op.drop_column("tool_calls", "error_category")
    op.drop_column("tool_calls", "attempt_count")
    op.drop_index("ix_agent_runs_claimable", table_name="agent_runs")
    op.drop_index("ix_agent_runs_next_attempt_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_lease_expires_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_worker_id", table_name="agent_runs")
    op.drop_column("agent_runs", "failure_category")
    op.drop_column("agent_runs", "last_heartbeat_at")
    op.drop_column("agent_runs", "next_attempt_at")
    op.drop_column("agent_runs", "attempt_count")
    op.drop_column("agent_runs", "lease_expires_at")
    op.drop_column("agent_runs", "worker_id")
