"""add evidence summary and content hash

Revision ID: 0002_evidence_integrity
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_evidence_integrity"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("evidence", sa.Column("summary", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "evidence",
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("evidence", "content_hash")
    op.drop_column("evidence", "summary")
