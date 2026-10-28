"""Tables of the `testing` schema (docs/experts/test.md section 12).

Revision ID: t0001
Revises:
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "t0001"
down_revision = None
branch_labels = None
depends_on = None

S = "testing"
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create every table of the testing schema."""
    # DEFAULT: ids are plain text/bigint columns without foreign keys to raw.*, so the schema can
    # be migrated and rebuilt independently of the collector's tables.
    op.create_table(
        "test_ids",
        sa.Column("test_id", sa.Text, primary_key=True),  # sha1(repo \0 file \0 full_name)
        sa.Column("repo", sa.Text, nullable=False),
        sa.Column("file", sa.Text, nullable=False, server_default=""),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("granularity", sa.Text, nullable=False),
        schema=S,
    )
    op.create_index("ix_test_ids_repo", "test_ids", ["repo"], schema=S)
    op.create_table(
        "test_results",
        sa.Column("job_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("test_id", sa.Text, primary_key=True),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("file", sa.Text),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("duration_ms", sa.Float),
        sa.Column("granularity", sa.Text, nullable=False),
        sa.CheckConstraint("granularity in ('test','file')", name="ck_test_results_granularity"),
        schema=S,
    )
    op.create_index("ix_test_results_test_id", "test_results", ["test_id"], schema=S)
    # DEFAULT: one row per parsed log, so extraction is resumable and parse failures can be counted
    # (a log with no test rows would otherwise look like one never tried). Not in section 12.
    op.create_table(
        "log_parses",
        sa.Column("job_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("repo", sa.Text, nullable=False),
        sa.Column("head_sha", sa.Text, nullable=False),
        sa.Column("job_name", sa.Text),
        sa.Column("reason", sa.Text, nullable=False),  # seed_failed | sibling
        sa.Column("framework", sa.Text),
        sa.Column("granularity", sa.Text),
        sa.Column("parsed_ok", sa.Boolean, nullable=False),
        sa.Column("is_test_job", sa.Boolean, nullable=False),
        sa.Column("confidence", sa.Float),
        sa.Column("n_rows", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),  # why it failed: missing_file, unreadable, parse_error, ...
        sa.Column("parsed_at", TZ, nullable=False, server_default=sa.func.now()),
        schema=S,
    )
    op.create_table(
        "labels",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("job_id", sa.BigInteger, nullable=False),
        sa.Column("sub_label", sa.Text),
        sa.Column("rule_name", sa.Text, nullable=False),
        sa.Column("confidence", sa.Text, nullable=False),
        sa.Column("labelled_by", sa.Text, nullable=False),
        sa.Column("is_final", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        schema=S,
    )
    op.create_index("ix_labels_job", "labels", ["job_id"], schema=S)
    op.create_table(
        "prerun_features",
        sa.Column("job_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("dataset_version", sa.Text, primary_key=True),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("features", postgresql.JSONB, nullable=False),
        schema=S,
    )
    op.create_table(
        "triage_features",
        sa.Column("failure_id", sa.Text, primary_key=True),
        sa.Column("features", postgresql.JSONB, nullable=False),
        sa.Column("verdict", sa.Text),
        sa.Column("confidence", sa.Float),
        schema=S,
    )
    op.create_table(
        "kb_cases",
        sa.Column("case_id", sa.Text, primary_key=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        schema=S,
    )
    # vector and tsvector have no portable SQLAlchemy type here, so plain SQL.
    op.execute(f"ALTER TABLE {S}.kb_cases ADD COLUMN embedding vector(384)")
    op.execute(f"ALTER TABLE {S}.kb_cases ADD COLUMN search tsvector")
    op.execute(f"CREATE INDEX ix_kb_cases_search ON {S}.kb_cases USING gin (search)")


def downgrade() -> None:
    """Drop every table of the testing schema."""
    for table in (
        "kb_cases",
        "triage_features",
        "prerun_features",
        "labels",
        "log_parses",
        "test_results",
        "test_ids",
    ):
        op.drop_table(table, schema=S)
