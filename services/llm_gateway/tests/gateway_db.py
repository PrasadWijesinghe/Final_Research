"""Throwaway migrated database for the gateway SQL test (`engine` fixture, imported by the test).

A helper module, not a conftest: a second `conftest` module would clash with the collector's
under mypy (`mypy_path` lists every tests folder).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

import shared.db
from shared.db.session import get_database_url

# DEFAULT: copied from api_db.py; cross-package test imports clash under pytest import mode
ALEMBIC_INI = Path(shared.db.__file__).resolve().parent / "alembic.ini"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A throwaway `<dbname>_gateway_test` database, migrated, dropped at the end."""
    main = make_url(get_database_url())
    assert main.database, "DATABASE_URL has no database name"
    name = f"{main.database}_gateway_test"
    url = main.set(database=name)
    admin = create_engine(main.set(database="postgres"), isolation_level="AUTOCOMMIT")
    quoted = admin.dialect.identifier_preparer.quote(name)
    eng: Engine | None = None
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {quoted}"))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
            command.upgrade(Config(str(ALEMBIC_INI)), "head")
        eng = create_engine(url)
        yield eng
    finally:
        if eng is not None:
            eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
        admin.dispose()
