"""enforce one approval decision per run

Revision ID: 0003_approval_idempotency
Revises: 0002_evidence_integrity
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_approval_idempotency"
down_revision: str | None = "0002_evidence_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ux_approvals_run_pk", "approvals", ["run_pk"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_approvals_run_pk", table_name="approvals")
