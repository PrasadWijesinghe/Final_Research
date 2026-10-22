"""Create the raw and platform tables (P0.6).

Revision ID: 0001
Revises:
Create Date: 2026-10-17
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)
BIGINT = sa.BigInteger()
FALSE = sa.text("false")
EMPTY = sa.text("'{}'")


def payload() -> sa.Column[Any]:
    """The jsonb column holding the whole contract message."""
    return sa.Column("payload", pg.JSONB, nullable=False)


def uuid_pk(name: str) -> sa.Column[Any]:
    """A UUID primary-key column."""
    return sa.Column(name, pg.UUID(as_uuid=True), primary_key=True)


def uuid_col(name: str) -> sa.Column[Any]:
    """A required UUID column."""
    return sa.Column(name, pg.UUID(as_uuid=True), nullable=False)


def upgrade() -> None:
    """Create extension, schemas and all raw/platform tables."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS raw")
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")

    # ---- raw
    op.create_table(
        "repos",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=False),
        sa.Column("full_name", sa.Text, nullable=False, unique=True),
        sa.Column("member", sa.Text),
        sa.Column("licence", sa.Text),
        sa.Column("default_branch", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        schema="raw",
    )
    op.create_table(
        "sync_state",
        sa.Column("repo_id", BIGINT, sa.ForeignKey("raw.repos.id"), primary_key=True),
        sa.Column("backfill_done_at", TZ),
        sa.Column("backfill_cursor", sa.Text),
        sa.Column("last_daily_sync_at", TZ),
        schema="raw",
    )
    op.create_table(
        "workflows",
        sa.Column("repo_id", BIGINT, sa.ForeignKey("raw.repos.id"), primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("relevant", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("detected_at_sha", sa.Text),
        schema="raw",
    )
    op.create_table(
        "workflow_runs",
        sa.Column("run_id", BIGINT, primary_key=True, autoincrement=False),
        sa.Column("attempt", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("repo_id", BIGINT, sa.ForeignKey("raw.repos.id"), nullable=False),
        sa.Column("workflow_path", sa.Text),
        sa.Column("head_sha", sa.Text, nullable=False),
        sa.Column("head_branch", sa.Text),
        sa.Column("event", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("conclusion", sa.Text),
        sa.Column("created_at", TZ),
        sa.Column("run_started_at", TZ),
        sa.Column("updated_at", TZ),
        sa.Column("actor_hash", sa.Text),
        sa.Column("pr_numbers", sa.ARRAY(BIGINT), nullable=False, server_default=EMPTY),
        sa.Column("source", sa.Text, nullable=False, server_default="collector"),
        sa.Column("raw", pg.JSONB),
        schema="raw",
    )
    op.create_index(
        "ix_workflow_runs_repo_created", "workflow_runs", ["repo_id", "created_at"], schema="raw"
    )
    op.create_index(
        "ix_workflow_runs_head_sha", "workflow_runs", ["repo_id", "head_sha"], schema="raw"
    )
    op.create_table(
        "jobs",
        sa.Column("job_id", BIGINT, primary_key=True, autoincrement=False),
        sa.Column("run_id", BIGINT, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("name", sa.Text),
        sa.Column("job_key", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("conclusion", sa.Text),
        sa.Column("started_at", TZ),
        sa.Column("completed_at", TZ),
        sa.Column("runner_os", sa.Text),
        sa.Column("labels", sa.ARRAY(sa.Text), nullable=False, server_default=EMPTY),
        sa.Column("raw", pg.JSONB),
        schema="raw",
    )
    op.create_index("ix_jobs_run_attempt", "jobs", ["run_id", "attempt"], schema="raw")
    op.create_table(
        "steps",
        sa.Column("job_id", BIGINT, sa.ForeignKey("raw.jobs.job_id"), primary_key=True),
        sa.Column("number", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("name", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("conclusion", sa.Text),
        sa.Column("started_at", TZ),
        sa.Column("completed_at", TZ),
        schema="raw",
    )
    op.create_table(
        "commits",
        sa.Column("repo_id", BIGINT, sa.ForeignKey("raw.repos.id"), primary_key=True),
        sa.Column("sha", sa.Text, primary_key=True),
        sa.Column("parent_shas", sa.ARRAY(sa.Text), nullable=False, server_default=EMPTY),
        sa.Column("author_hash", sa.Text),
        sa.Column("committed_at", TZ),
        sa.Column("message", sa.Text),
        schema="raw",
    )
    op.create_table(
        "commit_files",
        sa.Column("repo_id", BIGINT, primary_key=True, autoincrement=False),
        sa.Column("sha", sa.Text, primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("previous_path", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("additions", sa.Integer),
        sa.Column("deletions", sa.Integer),
        sa.Column("patch", sa.Text),
        sa.Column("patch_truncated", sa.Boolean, nullable=False, server_default=FALSE),
        sa.ForeignKeyConstraint(["repo_id", "sha"], ["raw.commits.repo_id", "raw.commits.sha"]),
        schema="raw",
    )
    op.create_table(
        "file_snapshots",
        sa.Column("repo_id", BIGINT, sa.ForeignKey("raw.repos.id"), primary_key=True),
        sa.Column("sha", sa.Text, primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("content", sa.Text),
        sa.Column("storage_path", sa.Text),
        sa.Column("fetched_at", TZ),
        schema="raw",
    )
    op.create_table(
        "logs",
        sa.Column("job_id", BIGINT, sa.ForeignKey("raw.jobs.job_id"), primary_key=True),
        sa.Column("storage_path", sa.Text),
        sa.Column("bytes", BIGINT),
        sa.Column("sha256", sa.String(64)),
        sa.Column("redactions", sa.Integer),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("fetched_at", TZ),
        schema="raw",
    )

    # ---- platform
    op.create_table(
        "commit_contexts",
        uuid_pk("context_id"),
        sa.Column("repo", sa.Text, nullable=False),
        sa.Column("head_sha", sa.Text, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("history_cutoff", TZ, nullable=False),
        payload(),
        schema="platform",
    )
    op.create_index(
        "ix_commit_contexts_repo_sha", "commit_contexts", ["repo", "head_sha"], schema="platform"
    )
    op.create_table(
        "predictions",
        uuid_pk("prediction_id"),
        uuid_col("context_id"),
        sa.Column("expert", sa.Text, nullable=False),
        sa.Column("probability", sa.Float),
        sa.Column("alert", sa.Boolean, nullable=False),
        sa.Column("is_replay", sa.Boolean, nullable=False, server_default=FALSE),
        payload(),
        schema="platform",
    )
    op.create_index("ix_predictions_context", "predictions", ["context_id"], schema="platform")
    op.create_table(
        "routed_failures",
        uuid_pk("failure_id"),
        sa.Column("run_id", BIGINT, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("job_id", BIGINT, nullable=False),
        sa.Column("replay", sa.Boolean, nullable=False, server_default=FALSE),
        payload(),
        schema="platform",
    )
    op.create_index(
        "ix_routed_failures_job",
        "routed_failures",
        ["run_id", "attempt", "job_id"],
        schema="platform",
    )
    op.create_table(
        "diagnoses",
        uuid_pk("diagnosis_id"),
        uuid_col("failure_id"),
        sa.Column("expert", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        payload(),
        schema="platform",
    )
    op.create_index("ix_diagnoses_failure", "diagnoses", ["failure_id"], schema="platform")
    op.create_table(
        "reviews",
        uuid_pk("decision_id"),
        uuid_col("diagnosis_id"),
        sa.Column("decision", sa.Text, nullable=False),
        payload(),
        schema="platform",
    )
    op.create_index("ix_reviews_diagnosis", "reviews", ["diagnosis_id"], schema="platform")
    op.create_table(
        "failure_labels",
        sa.Column("id", BIGINT, sa.Identity(), primary_key=True),
        sa.Column("run_id", BIGINT, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("job_id", BIGINT, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("sub_label", sa.Text),
        sa.Column("labelled_by", sa.Text, nullable=False),
        sa.Column("is_final", sa.Boolean, nullable=False, server_default=FALSE),
        payload(),
        schema="platform",
    )
    op.create_index(
        "ix_failure_labels_job",
        "failure_labels",
        ["run_id", "attempt", "job_id"],
        schema="platform",
    )
    op.create_table(
        "api_keys",
        uuid_pk("id"),
        sa.Column("user_hash", sa.Text, nullable=False),
        sa.Column("key_hash", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", TZ),
        schema="platform",
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", BIGINT, sa.Identity(), primary_key=True),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("expert", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("cache_hit", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        schema="platform",
    )


PLATFORM_TABLES = [
    "llm_calls",
    "api_keys",
    "failure_labels",
    "reviews",
    "diagnoses",
    "routed_failures",
    "predictions",
    "commit_contexts",
]
RAW_TABLES = [
    "logs",
    "file_snapshots",
    "commit_files",
    "commits",
    "steps",
    "jobs",
    "workflow_runs",
    "workflows",
    "sync_state",
    "repos",
]


def downgrade() -> None:
    """Drop the tables (schemas and the vector extension are left in place)."""
    # DEFAULT: schemas and extension are shared with other migrations/init script, so not dropped.
    for table in PLATFORM_TABLES:
        op.drop_table(table, schema="platform")
    for table in RAW_TABLES:
        op.drop_table(table, schema="raw")
