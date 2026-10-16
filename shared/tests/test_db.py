"""Database layer tests: migrations up/down and one round-trip row per table."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.db import models as m
from shared.db.session import get_database_url, get_engine

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "db" / "alembic.ini"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _config() -> Config:
    return Config(str(ALEMBIC_INI))


def test_missing_database_url_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        get_database_url()


def test_every_model_table_is_in_raw_or_platform() -> None:
    schemas = {t.schema for t in m.Base.metadata.sorted_tables}
    assert schemas == {"raw", "platform"}
    assert len(m.Base.metadata.sorted_tables) == 18


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Create a throwaway `<dbname>_test` database, migrate it, and drop it at session end.

    Integration tests only ever touch this database, never the one in DATABASE_URL.
    """
    # DEFAULT: the throwaway database is named `<main name>_test` and is created through the
    # maintenance database `postgres`; the DATABASE_URL user needs the CREATEDB right.
    main = make_url(get_database_url())
    assert main.database, "DATABASE_URL has no database name"
    test_name = f"{main.database}_test"
    test_url = main.set(database=test_name)
    admin = create_engine(main.set(database="postgres"), isolation_level="AUTOCOMMIT")
    quoted = admin.dialect.identifier_preparer.quote(test_name)
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {quoted}"))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("DATABASE_URL", test_url.render_as_string(hide_password=False))
            command.upgrade(_config(), "head")
        yield test_url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def engine(test_database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Engine on the throwaway database, with DATABASE_URL pointing at it for Alembic."""
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    eng = get_engine()
    command.upgrade(_config(), "head")
    yield eng
    eng.dispose()


@pytest.mark.integration
def test_engine_fixture_never_points_at_main_database(engine: Engine) -> None:
    assert engine.url.database is not None and engine.url.database.endswith("_test")


@pytest.mark.integration
def test_alembic_check_reports_no_drift(engine: Engine) -> None:
    # Raises CommandError when the models and the migrations disagree.
    command.check(_config())


@pytest.mark.integration
def test_upgrade_downgrade_upgrade(engine: Engine) -> None:
    cfg = _config()
    command.downgrade(cfg, "base")
    insp = inspect(engine)
    assert insp.get_table_names(schema="raw") == []
    assert insp.get_table_names(schema="platform") == []
    command.upgrade(cfg, "head")
    insp = inspect(engine)
    expected = {t.name for t in m.Base.metadata.sorted_tables if t.schema == "raw"}
    assert expected <= set(insp.get_table_names(schema="raw"))
    expected = {t.name for t in m.Base.metadata.sorted_tables if t.schema == "platform"}
    assert expected <= set(insp.get_table_names(schema="platform"))


@pytest.mark.integration
def test_extension_and_schemas_exist(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar()
        assert conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname='raw'")).scalar()


@pytest.mark.integration
def test_round_trip_one_row_per_table(engine: Engine) -> None:
    ids = {name: uuid.uuid4() for name in ("ctx", "pred", "fail", "diag", "dec", "key")}
    # DEFAULT: odd ids far from real GitHub ids; everything is rolled back at the end anyway.
    repo_id, run_id, job_id = 999_000_001, 999_000_002, 999_000_003
    with Session(engine) as session:
        try:
            rows: list[object] = [
                m.Repo(id=repo_id, full_name="octo/roundtrip", member="test"),
                m.SyncState(repo_id=repo_id, backfill_cursor="c1", last_daily_sync_at=NOW),
                m.Workflow(repo_id=repo_id, path=".github/workflows/ci.yml", relevant=True),
                m.WorkflowRun(
                    run_id=run_id,
                    attempt=1,
                    repo_id=repo_id,
                    head_sha="abc",
                    pr_numbers=[1, 2],
                    created_at=NOW,
                    raw={"k": "v"},
                ),
                m.Job(job_id=job_id, run_id=run_id, attempt=1, labels=["ubuntu"], raw={}),
                m.Step(job_id=job_id, number=1, name="checkout"),
                m.Commit(repo_id=repo_id, sha="abc", parent_shas=["p1"], message="msg"),
                m.CommitFile(repo_id=repo_id, sha="abc", path="a.js", additions=1, deletions=0),
                m.FileSnapshot(repo_id=repo_id, sha="abc", path="a.js", content="x"),
                m.Log(job_id=job_id, status="ok", bytes=3, sha256="0" * 64, redactions=0),
                m.CommitContextRow(
                    context_id=ids["ctx"],
                    repo="octo/roundtrip",
                    head_sha="abc",
                    event="push",
                    history_cutoff=NOW,
                    payload={"a": 1},
                ),
                m.PredictionRow(
                    prediction_id=ids["pred"],
                    context_id=ids["ctx"],
                    expert="test",
                    probability=0.5,
                    alert=False,
                    payload={},
                ),
                m.RoutedFailureRow(
                    failure_id=ids["fail"],
                    run_id=run_id,
                    attempt=1,
                    job_id=job_id,
                    payload={},
                ),
                m.DiagnosisRow(
                    diagnosis_id=ids["diag"],
                    failure_id=ids["fail"],
                    expert="test",
                    status="diagnosed",
                    payload={},
                ),
                m.ReviewRow(
                    decision_id=ids["dec"],
                    diagnosis_id=ids["diag"],
                    decision="accept",
                    payload={},
                ),
                m.FailureLabelRow(
                    run_id=run_id,
                    attempt=1,
                    job_id=job_id,
                    category="test",
                    labelled_by="rule",
                    is_final=True,
                    payload={},
                ),
                m.ApiKey(id=ids["key"], user_hash="u", key_hash="k" + ids["key"].hex),
                m.LlmCall(
                    expert="test",
                    model="mock",
                    input_tokens=1,
                    output_tokens=2,
                    cost_usd=Decimal("0.001"),
                    latency_ms=5,
                ),
            ]
            # No ORM relationships exist, so flush one row at a time in foreign-key order.
            for row in rows:
                session.add(row)
                session.flush()
            assert len(rows) == 18
            session.expire_all()
            run = session.get(m.WorkflowRun, (run_id, 1))
            assert run is not None and run.pr_numbers == [1, 2] and run.raw == {"k": "v"}
            assert run.created_at == NOW and run.source == "collector"
            commit = session.get(m.Commit, (repo_id, "abc"))
            assert commit is not None and commit.parent_shas == ["p1"]
            for table in m.Base.metadata.sorted_tables:
                count = session.execute(text(f"SELECT count(*) FROM {table.fullname}")).scalar()
                assert count == 1, table.fullname
            key = session.get(m.ApiKey, ids["key"])
            assert key is not None and key.created_at is not None
        finally:
            session.rollback()
    # Nothing was committed, so the table is clean for the next run.
    with Session(engine) as session:
        assert session.get(m.Repo, repo_id) is None


def test_database_url_is_never_hard_coded() -> None:
    ini = ALEMBIC_INI.read_text(encoding="utf-8")
    assert "postgresql" not in ini
