"""Knowledge-base columns, vector index and lookups for `testing.kb_cases` (P4.10).

Revision ID: t0004
Revises: t0003
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "t0004"
down_revision = "t0003"
branch_labels = None
depends_on = None

S = "testing"
TZ = sa.DateTime(timezone=True)

_COLUMNS: list[sa.Column] = [  # type: ignore[type-arg]
    # DEFAULT: `payload` stays exactly the VerifiedCase JSON (the contract). Everything the contract
    # has no field for (links, times, the diff excerpt, what was embedded) gets its own column.
    sa.Column("kind", sa.Text),  # genuine | flaky
    sa.Column("repo", sa.Text),
    sa.Column("licence", sa.Text),
    sa.Column("failed_job_id", sa.BigInteger),
    sa.Column("failed_run_id", sa.BigInteger),
    sa.Column("failed_sha", sa.Text),
    sa.Column("failed_at", TZ),  # when the failing job finished
    sa.Column("verified_at", TZ),  # when the case became known (fix passed / re-run passed)
    sa.Column("fix_sha", sa.Text),
    sa.Column("error_family", sa.Text),
    sa.Column("failing_tests", postgresql.ARRAY(sa.Text)),
    sa.Column("search_text", sa.Text),  # the text that is indexed and embedded
    sa.Column("text_hash", sa.Text),  # sha1 of search_text: a changed text is re-embedded
    sa.Column("diff_excerpt", sa.Text),
    sa.Column("links", postgresql.JSONB),
    sa.Column("mine_rule", sa.Text),  # label rule the case came from
    sa.Column("embedding_model", sa.Text),
    sa.Column("embedded_at", TZ),
]


def upgrade() -> None:
    """Add the case columns and the indexes retrieval uses."""
    for col in _COLUMNS:
        op.add_column("kb_cases", col, schema=S)
    # The tsvector column and its GIN index come from t0001; add the vector index and lookups.
    # DEFAULT: HNSW with cosine distance, like shared/embeddings (no training step).
    op.execute(
        f"CREATE INDEX ix_kb_cases_embedding ON {S}.kb_cases "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(f"CREATE INDEX ix_kb_cases_tags ON {S}.kb_cases USING gin (tags)")
    op.create_index("ix_kb_cases_repo", "kb_cases", ["repo"], schema=S)
    op.create_index("ix_kb_cases_verified_at", "kb_cases", ["verified_at"], schema=S)
    op.create_index("ix_kb_cases_failed_run", "kb_cases", ["failed_run_id"], schema=S)


def downgrade() -> None:
    """Drop the indexes and columns again."""
    for name in (
        "ix_kb_cases_failed_run",
        "ix_kb_cases_verified_at",
        "ix_kb_cases_repo",
        "ix_kb_cases_tags",
        "ix_kb_cases_embedding",
    ):
        op.execute(f"DROP INDEX IF EXISTS {S}.{name}")
    for col in reversed(_COLUMNS):
        op.drop_column("kb_cases", col.name, schema=S)
