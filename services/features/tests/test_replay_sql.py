"""SqlHistory against Postgres (integration: skipped when the database is unreachable).

Seeds the scenario of `ctx_scenario` into the `raw` tables and checks that replaying from SQL gives
the same context as replaying from the in-memory history.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import ctx_scenario as sc
import pytest
from alembic import command
from alembic.config import Config
from features.replay import SqlHistory, build_replay_context
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import shared.db
from shared.db import models as m
from shared.db.session import get_database_url

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(shared.db.__file__).resolve().parent / "alembic.ini"
REPO_ID = 42


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A throwaway `<dbname>_features_test` database, migrated, dropped at the end."""
    main = make_url(get_database_url())
    assert main.database, "DATABASE_URL has no database name"
    name = f"{main.database}_features_test"
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


def seed(engine: Engine) -> None:
    memory = sc.history()
    with Session(engine) as s:
        s.add(m.Repo(id=REPO_ID, full_name=sc.REPO, member="test"))
        s.flush()
        for _, r in memory.runs:
            s.add(
                m.WorkflowRun(
                    run_id=r.run_id,
                    attempt=r.attempt,
                    repo_id=REPO_ID,
                    workflow_path=r.workflow_path,
                    head_sha=r.head_sha,
                    head_branch=r.head_branch,
                    created_at=r.created_at,
                    actor_hash=r.actor_hash,
                    pr_numbers=list(r.pr_numbers),
                )
            )
        c = memory.commits[(sc.REPO, sc.HEAD)]
        s.add(
            m.Commit(
                repo_id=REPO_ID,
                sha=c.sha,
                parent_shas=list(c.parent_shas),
                author_hash=c.author_hash,
                committed_at=c.committed_at,
            )
        )
        s.flush()
        for f in memory.files[(sc.REPO, sc.HEAD)]:
            s.add(
                m.CommitFile(
                    repo_id=REPO_ID,
                    sha=sc.HEAD,
                    path=f.path,
                    previous_path=f.previous_path,
                    status=f.status,
                    additions=f.additions,
                    deletions=f.deletions,
                    patch=f.patch,
                    patch_truncated=f.patch_truncated,
                )
            )
        for (_, sha, path), snap in memory.snapshots.items():
            s.add(
                m.FileSnapshot(
                    repo_id=REPO_ID,
                    sha=sha,
                    path=path,
                    content=snap.content,
                    storage_path=snap.storage_path,
                )
            )
        s.commit()


def test_sql_history_replays_like_memory(engine: Engine) -> None:
    seed(engine)
    from_sql = build_replay_context(SqlHistory(engine), repo=sc.REPO, run_id=sc.RUN_ID, attempt=1)
    from_memory = build_replay_context(sc.history(), repo=sc.REPO, run_id=sc.RUN_ID, attempt=1)
    skip = {"context_id"}
    assert from_sql.model_dump(mode="json", exclude=skip) == from_memory.model_dump(
        mode="json", exclude=skip
    )
    assert from_sql.history_cutoff == sc.RUN_CREATED
    # a run of another repo name is not found
    history = SqlHistory(engine)
    assert history.get_run("other/repo", sc.RUN_ID, 1) is None
    assert history.get_run(sc.REPO, 999, 1) is None
    # strictly before the cutoff
    assert history.runs_for_sha(sc.REPO, sc.HEAD, sc.RUN_CREATED) == []
