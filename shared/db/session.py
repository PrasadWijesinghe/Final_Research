"""Database engine helper: the URL comes only from the DATABASE_URL environment variable."""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine


def get_database_url() -> str:
    """Return DATABASE_URL, or raise a clear error when it is not set."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env (make loads it) "
            "or export DATABASE_URL "
            "(e.g. postgresql+psycopg://user:password@127.0.0.1:5432/pipelineiq)."
        )
    return url


def get_engine() -> Engine:
    """Create a SQLAlchemy engine for DATABASE_URL."""
    return create_engine(get_database_url())
