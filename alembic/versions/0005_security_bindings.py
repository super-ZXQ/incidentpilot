"""Resource ownership and tested patch bindings.

Revision ID: 0005_security_bindings
Revises: 0004_worker_leases
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_security_bindings"
down_revision = "0004_worker_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("incidents", sa.Column("owner_id", sa.String(128), nullable=True))
    for name, size, default in (("patch_hash", 64, ""), ("base_commit_sha", 64, ""), ("mode", 32, "unknown")):
        op.add_column("test_runs", sa.Column(name, sa.String(size), nullable=False, server_default=default))


def downgrade():
    for name in ("mode", "base_commit_sha", "patch_hash"):
        op.drop_column("test_runs", name)
    op.drop_column("incidents", "owner_id")
