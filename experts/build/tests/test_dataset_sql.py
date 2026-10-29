"""Dataset builder against Postgres (integration: skipped when the database is unreachable).

Seeds the scenario of `build_scenario` into the raw tables, writes its logs as gzip files the way
the collector does, and checks that building from SQL gives exactly the in-memory result.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import build_scenario as sc
import pyarrow.parquet as pq
import pytest
from alembic import command
from alembic.config import Config
from build_expert.dataset import DatasetSettings, ListLabelSink, SqlDatasetSource, build_dataset
from build_expert.dataset.labels_sql import SqlLabelSink
from features.replay import SqlHistory
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import shared.db
from shared.db import models as m
from shared.db.session import get_database_url
from shared.file_tags import FileTagger

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(shared.db.__file__).resolve().parent / "alembic.ini"
TAGS = Path(__file__).resolve().parents[3] / "config" / "file_tags.yaml"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A throwaway `<dbname>_build_dataset_test` database, migrated, dropped at the end."""
    try:
        main = make_url(get_database_url())
    except RuntimeError:
        pytest.skip("DATABASE_URL is not set")
    assert main.database, "DATABASE_URL has no database name"
    name = f"{main.database}_build_dataset_test"
    admin = create_engine(main.set(database="postgres"), isolation_level="AUTOCOMMIT")
    quoted = admin.dialect.identifier_preparer.quote(name)
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {quoted}"))
    except OperationalError:
        admin.dispose()
        pytest.skip("database in DATABASE_URL is unreachable (run `make up`)")
    url = main.set(database=name)
    eng: Engine | None = None
    try:
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


def seed(engine: Engine, data_dir: Path) -> None:
    """Write the scenario into the raw tables and its logs under data_dir."""
    history = sc.history()
    with Session(engine) as s, s.begin():
        s.add(
            m.Repo(
                id=sc.REPO_ID,
                full_name=sc.REPO,
                member="build",
                licence=sc.LICENCE,
                default_branch=sc.DEFAULT_BRANCH,
            )
        )
        s.flush()
        s.add(m.Workflow(repo_id=sc.REPO_ID, path=sc.CI, name="CI", relevant=True))
        for r in sc.runs():
            s.add(
                m.WorkflowRun(
                    run_id=r.run_id,
                    attempt=r.attempt,
                    repo_id=sc.REPO_ID,
                    workflow_path=r.workflow_path,
                    head_sha=r.head_sha,
                    head_branch=r.head_branch,
                    event=r.event,
                    status=r.status,
                    conclusion=r.conclusion,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                    pr_numbers=list(r.pr_numbers),
                )
            )
        for (_, sha), c in history.commits.items():
            s.add(
                m.Commit(
                    repo_id=sc.REPO_ID,
                    sha=sha,
                    parent_shas=list(c.parent_shas),
                    author_hash=c.author_hash,
                    committed_at=c.committed_at,
                    message=next(x.message for x in sc.COMMITS if sc.sha(x.n) == sha),
                )
            )
        s.flush()
        for (_, sha), files in history.files.items():
            for f in files:
                s.add(
                    m.CommitFile(
                        repo_id=sc.REPO_ID,
                        sha=sha,
                        path=f.path,
                        status=f.status,
                        additions=f.additions,
                        deletions=f.deletions,
                        patch=f.patch,
                    )
                )
        for (_, sha, path), snap in history.snapshots.items():
            s.add(m.FileSnapshot(repo_id=sc.REPO_ID, sha=sha, path=path, content=snap.content))
        logs = sc.log_texts()
        for j in sc.jobs():
            s.add(
                m.Job(
                    job_id=j.job_id,
                    run_id=j.run_id,
                    attempt=j.attempt,
                    name=j.name,
                    job_key=j.job_key,
                    status=j.status,
                    conclusion=j.conclusion,
                )
            )
            s.flush()
            for st in j.steps:
                s.add(
                    m.Step(
                        job_id=j.job_id, number=st.number, name=st.name, conclusion=st.conclusion
                    )
                )
            if j.log_path:
                if j.log_status == "ok":
                    target = data_dir / j.log_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(gzip.compress(logs[j.log_path].encode("utf-8")))
                s.add(m.Log(job_id=j.job_id, storage_path=j.log_path, status=j.log_status or "ok"))


def test_sql_build_matches_memory_and_stores_labels(engine: Engine, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    seed(engine, data_dir)
    tagger = FileTagger.load(TAGS)
    sql_out, mem_out = tmp_path / "sql", tmp_path / "mem"
    summary = build_dataset(
        SqlDatasetSource(engine, data_dir),
        SqlHistory(engine),
        sql_out,
        DatasetSettings(version="v1"),
        tagger=tagger,
        label_sink=SqlLabelSink(engine),
    )
    build_dataset(
        sc.source(),
        sc.history(),
        mem_out,
        DatasetSettings(version="v1"),
        tagger=tagger,
        label_sink=ListLabelSink(),
    )
    sql_rows = pq.read_table(sql_out / "v1" / "rows.parquet").to_pylist()
    mem_rows = pq.read_table(mem_out / "v1" / "rows.parquet").to_pylist()
    assert summary.rows == 9
    assert sorted(sql_rows, key=lambda r: r["head_sha"]) == sorted(
        mem_rows, key=lambda r: r["head_sha"]
    )
    sql_fix = pq.read_table(sql_out / "v1" / "fix_candidates.parquet").to_pylist()
    assert [(f["failed_sha"], f["fix_sha"]) for f in sql_fix] == [(sc.sha(2), sc.sha(3))]

    with Session(engine) as s:
        rows = s.scalars(select(m.FailureLabelRow).order_by(m.FailureLabelRow.job_id)).all()
        assert [(r.job_id, r.sub_label, r.is_final) for r in rows] == [
            (500210, "tsc", False),
            (500410, "eslint", False),
            (500421, "eslint", False),
            (500610, "bundler", False),
            (501010, "eslint", False),
        ]
        assert rows[0].payload["rule_name"] == "build.build_step_tool_error.v1"

    # A second build replaces the rule labels instead of adding duplicates, and keeps others.
    with Session(engine) as s, s.begin():
        s.add(
            m.FailureLabelRow(
                run_id=5005,
                attempt=1,
                job_id=500511,
                category="test",
                labelled_by="rule",
                payload={},
            )
        )
    build_dataset(
        SqlDatasetSource(engine, data_dir),
        SqlHistory(engine),
        sql_out,
        DatasetSettings(version="v2"),
        tagger=tagger,
        label_sink=SqlLabelSink(engine),
    )
    with Session(engine) as s:
        count = s.scalar(select(func.count()).select_from(m.FailureLabelRow))
        build = s.scalar(
            select(func.count())
            .select_from(m.FailureLabelRow)
            .where(m.FailureLabelRow.category == "build")
        )
    assert (count, build) == (6, 5)
