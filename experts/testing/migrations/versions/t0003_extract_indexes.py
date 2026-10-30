"""Indexes for full-scope test-results extraction and per-test history (P4.2, P4.4).

Revision ID: t0003
Revises: t0002
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op

revision = "t0003"
down_revision = "t0002"
branch_labels = None
depends_on = None

S = "testing"


def upgrade() -> None:
    """Add lookups used by the history loader."""
    # DEFAULT: history reads results per repo and job name / head sha; test_results is already
    # indexed by test_id and (job_id, test_id), and test_ids by repo.
    op.create_index("ix_log_parses_repo_job", "log_parses", ["repo", "job_name"], schema=S)
    op.create_index("ix_log_parses_run", "log_parses", ["run_id", "attempt"], schema=S)
    op.create_index("ix_test_ids_repo_file", "test_ids", ["repo", "file"], schema=S)


def downgrade() -> None:
    """Drop the indexes."""
    op.drop_index("ix_test_ids_repo_file", table_name="test_ids", schema=S)
    op.drop_index("ix_log_parses_run", table_name="log_parses", schema=S)
    op.drop_index("ix_log_parses_repo_job", table_name="log_parses", schema=S)
