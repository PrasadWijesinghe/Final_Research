"""Table platform.check_runs: the GitHub check run PipelineIQ opened per (repo, head_sha).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create platform.check_runs."""
    op.create_table(
        "check_runs",
        sa.Column("repo", sa.Text, primary_key=True),
        sa.Column("head_sha", sa.Text, primary_key=True),
        sa.Column("check_run_id", sa.BigInteger, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="platform",
    )


def downgrade() -> None:
    """Drop platform.check_runs."""
    op.drop_table("check_runs", schema="platform")
