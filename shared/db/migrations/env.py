"""Alembic environment: online migrations against DATABASE_URL."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from shared.db.models import Base
from shared.db.session import get_database_url

target_metadata = Base.metadata


def run_migrations_online() -> None:
    """Run migrations with a live connection."""
    engine = create_engine(get_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


# DEFAULT: offline (SQL-script) mode is not supported; the team always migrates a live database.
run_migrations_online()
