"""Alembic environment: online migrations against DATABASE_URL."""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool

from shared.db.models import Base
from shared.db.session import get_database_url

target_metadata = Base.metadata

# DEFAULT: only the raw and platform schemas are managed here; expert schemas and the vector
# extension belong to other migrations or the init script, so they are ignored by autogenerate.
MANAGED_SCHEMAS = {"raw", "platform"}


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    """Limit schema comparison to the raw and platform schemas."""
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    return True


def run_migrations_online() -> None:
    """Run migrations with a live connection."""
    engine = create_engine(get_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


# DEFAULT: offline (SQL-script) mode is not supported; the team always migrates a live database.
run_migrations_online()
