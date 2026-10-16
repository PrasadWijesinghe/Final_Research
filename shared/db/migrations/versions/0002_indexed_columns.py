"""Single-column indexes for the columns DATA.md 4.2 lists as indexed (P0.6 follow-up).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# DEFAULT: index names are ix_platform_<table>_<column>, as SQLAlchemy names them.
# Columns already covered by a primary key, a unique key or the first column of an existing
# composite index (0001) are left out.
INDEXES: dict[str, list[str]] = {
    "commit_contexts": ["head_sha", "event", "history_cutoff"],
    "predictions": ["expert", "probability", "alert", "is_replay"],
    "routed_failures": ["attempt", "job_id", "replay"],
    "diagnoses": ["expert", "status"],
    "reviews": ["decision"],
    "failure_labels": ["attempt", "job_id", "category", "sub_label", "labelled_by", "is_final"],
    "api_keys": ["user_hash", "created_at", "revoked_at"],
    "llm_calls": [
        "expert",
        "model",
        "prompt_version",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "cache_hit",
        "latency_ms",
    ],
}


def upgrade() -> None:
    """Create the single-column indexes."""
    for table, columns in INDEXES.items():
        for column in columns:
            op.create_index(f"ix_platform_{table}_{column}", table, [column], schema="platform")


def downgrade() -> None:
    """Drop the single-column indexes."""
    for table, columns in INDEXES.items():
        for column in columns:
            op.drop_index(f"ix_platform_{table}_{column}", table_name=table, schema="platform")
