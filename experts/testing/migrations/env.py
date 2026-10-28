"""Alembic environment for the `testing` schema: online migrations against DATABASE_URL."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

from shared.db.session import get_database_url

SCHEMA = "testing"


def run_migrations_online() -> None:
    """Run migrations with a live connection."""
    engine = create_engine(get_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # DEFAULT: this expert keeps its own version table (testing.alembic_version) so its
        # history never mixes with shared/db's (public.alembic_version).
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table="alembic_version",
            version_table_schema=SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()


# DEFAULT: offline (SQL-script) mode is not supported; the team always migrates a live database.
run_migrations_online()
