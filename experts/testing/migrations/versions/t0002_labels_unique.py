"""One rule label and one manual label per job in testing.labels (P4.3 v1).

Revision ID: t0002
Revises: t0001
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "t0002"
down_revision = "t0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the unique index that keeps the labeller idempotent."""
    # DEFAULT: unique on (job_id, labelled_by): a job has at most one rule row and one manual row.
    op.create_index(
        "ux_labels_job_by", "labels", ["job_id", "labelled_by"], unique=True, schema="testing"
    )


def downgrade() -> None:
    """Drop the unique index."""
    op.drop_index("ux_labels_job_by", table_name="labels", schema="testing")
