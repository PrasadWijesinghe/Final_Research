"""Table raw.skipped_runs: runs the collector skipped after persistent GitHub 5xx errors.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create raw.skipped_runs."""
    op.create_table(
        "skipped_runs",
        sa.Column("repo_id", sa.BigInteger, sa.ForeignKey("raw.repos.id"), primary_key=True),
        sa.Column("run_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=False),
        schema="raw",
    )


def downgrade() -> None:
    """Drop raw.skipped_runs."""
    op.drop_table("skipped_runs", schema="raw")
