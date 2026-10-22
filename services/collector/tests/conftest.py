"""Collector test fixtures: the fake GitHub, a salt for hashing, and a throwaway database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from alembic import command
from alembic.config import Config
from recorded_github import FAKE_SALT, FakeGitHub
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

import shared.db
from shared.db import models as m
from shared.db.session import get_database_url, get_engine

ALEMBIC_INI = Path(shared.db.__file__).resolve().parent / "alembic.ini"


@pytest.fixture(autouse=True)
def identity_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    """hash_identity needs IDENTITY_SALT; tests use an obviously fake one."""
    monkeypatch.setenv("IDENTITY_SALT", FAKE_SALT)


@pytest.fixture
def fake_github() -> Iterator[FakeGitHub]:
    """Route all HTTP to the recorded fixtures for the duration of a test."""
    with respx.mock(assert_all_called=False) as router:
        yield FakeGitHub().install(router)


@pytest.fixture(scope="session")
def collector_database_url() -> Iterator[str]:
    """Create a throwaway `<dbname>_collector_test` database, migrate it, drop it at the end."""
    # DEFAULT: its own throwaway database (not shared/tests' `_test`) so the two test suites
    # can never drop each other's database; the DATABASE_URL user needs the CREATEDB right.
    main = make_url(get_database_url())
    assert main.database, "DATABASE_URL has no database name"
    name = f"{main.database}_collector_test"
    url = main.set(database=name)
    admin = create_engine(main.set(database="postgres"), isolation_level="AUTOCOMMIT")
    quoted = admin.dialect.identifier_preparer.quote(name)
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {quoted}"))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
            command.upgrade(Config(str(ALEMBIC_INI)), "head")
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def engine(collector_database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Engine on the throwaway database with every raw table emptied."""
    monkeypatch.setenv("DATABASE_URL", collector_database_url)
    eng = get_engine()
    raw = [t.fullname for t in m.Base.metadata.sorted_tables if t.schema == "raw"]
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(raw)} CASCADE"))
    yield eng
    eng.dispose()
