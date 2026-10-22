"""Backfill against Postgres (integration: skipped automatically when the database is down).

Same recorded GitHub fixtures as `test_backfill.py`, but the rows go through `SqlStore`
(INSERT ... ON CONFLICT) into a throwaway database with the real migrations applied.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from collector.store import (
    CommitFileRecord,
    CommitRecord,
    JobRecord,
    LogRecord,
    RepoRecord,
    RunRecord,
    SnapshotRecord,
    SqlStore,
    StepRecord,
    WorkflowRecord,
)
from memory_store import PRIMARY_KEYS, MemoryStore
from recorded_github import NOW, REPO_ID, FakeGitHub, SimulatedCrash, file_checksums
from scenario import ENTRY, check_expected_state, make_backfiller
from sqlalchemy import Engine, Table, select, text

from shared.db import models as m

pytestmark = pytest.mark.integration


def dump_db(engine: Engine) -> dict[str, list[dict[str, Any]]]:
    """Every raw table as column dicts sorted by primary key (same shape as MemoryStore.dump)."""
    out: dict[str, list[dict[str, Any]]] = {}
    with engine.connect() as conn:
        for name, keys in PRIMARY_KEYS.items():
            table: Table = m.Base.metadata.tables[f"raw.{name}"]
            rows = [dict(r) for r in conn.execute(select(table)).mappings()]
            out[name] = sorted(rows, key=lambda r: repr(tuple(r[k] for k in keys)))
    return out


def counts(engine: Engine) -> dict[str, int]:
    """Row count per raw table."""
    with engine.connect() as conn:
        return {
            name: int(conn.execute(text(f"SELECT count(*) FROM raw.{name}")).scalar_one())
            for name in PRIMARY_KEYS
        }


def test_backfill_creates_rows_and_files(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    make_backfiller(SqlStore(engine), tmp_path).backfill_repo(ENTRY)
    dump = dump_db(engine)
    check_expected_state(dump, tmp_path)
    assert counts(engine) == {
        "repos": 1, "sync_state": 1, "workflows": 2, "workflow_runs": 4, "jobs": 8,
        "steps": 32, "commits": 3, "commit_files": 9, "file_snapshots": 5, "logs": 7,
    }  # fmt: skip


def test_sql_and_memory_stores_end_in_the_same_state(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    memory = MemoryStore()
    make_backfiller(memory, tmp_path / "mem").backfill_repo(ENTRY)
    make_backfiller(SqlStore(engine), tmp_path / "sql").backfill_repo(ENTRY)
    sql_dump = dump_db(engine)
    for table, rows in memory.dump().items():
        assert sql_dump[table] == rows, table
    assert file_checksums(tmp_path / "mem") == file_checksums(tmp_path / "sql")


def test_running_twice_changes_nothing(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    make_backfiller(SqlStore(engine), tmp_path).backfill_repo(ENTRY)
    rows_before, counts_before = dump_db(engine), counts(engine)
    files_before = file_checksums(tmp_path)

    make_backfiller(SqlStore(engine), tmp_path).backfill_repo(ENTRY)

    assert counts(engine) == counts_before
    assert dump_db(engine) == rows_before
    assert file_checksums(tmp_path) == files_before


def test_rerun_from_scratch_over_existing_rows_is_idempotent(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    """Even without the 'already done' shortcut, upserting the same data changes nothing."""
    make_backfiller(SqlStore(engine), tmp_path).backfill_repo(ENTRY)
    before = dump_db(engine)
    with engine.begin() as conn:  # forget that runs were completed, keep the data
        conn.execute(text("UPDATE raw.workflow_runs SET status = 'in_progress'"))
        conn.execute(text("DELETE FROM raw.logs WHERE status = 'ok'"))
    make_backfiller(SqlStore(engine), tmp_path).backfill_repo(ENTRY)
    after = dump_db(engine)
    assert after == before
    assert counts(engine)["workflow_runs"] == 4


@pytest.mark.parametrize("crash_job", [5005, 5008])
def test_interrupted_backfill_resumes_to_the_same_state(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path, crash_job: int
) -> None:
    make_backfiller(SqlStore(engine), tmp_path / "clean").backfill_repo(ENTRY)
    clean_rows, clean_files = dump_db(engine), file_checksums(tmp_path / "clean")
    with engine.begin() as conn:
        raw = ", ".join(f"raw.{t}" for t in PRIMARY_KEYS)
        conn.execute(text(f"TRUNCATE {raw} CASCADE"))

    data = tmp_path / "interrupted"
    fake_github.crash_on_log = {crash_job}
    with pytest.raises(SimulatedCrash):
        make_backfiller(SqlStore(engine), data).backfill_repo(ENTRY)
    with engine.connect() as conn:
        done, cursor = conn.execute(
            text("SELECT backfill_done_at, backfill_cursor FROM raw.sync_state")
        ).one()
        # the interrupted run's rows were rolled back with it
        orphans = conn.execute(
            text(
                "SELECT count(*) FROM raw.jobs j WHERE NOT EXISTS "
                "(SELECT 1 FROM raw.workflow_runs r WHERE r.run_id = j.run_id)"
            )
        ).scalar_one()
    assert done is None and orphans == 0
    assert (cursor is not None) == (crash_job == 5008)

    make_backfiller(SqlStore(engine), data).backfill_repo(ENTRY)

    assert dump_db(engine) == clean_rows
    assert file_checksums(data) == clean_files
    check_expected_state(dump_db(engine), data)


# ---- SqlStore on its own: every method, with upsert (update) behaviour ----------------------


def test_sql_store_upserts_update_in_place(engine: Engine) -> None:
    store = SqlStore(engine)
    when = NOW
    with store.transaction():
        store.upsert_repo(RepoRecord(REPO_ID, "acme/widgets", "test", "MIT", "main"))
        store.upsert_repo(RepoRecord(REPO_ID, "acme/widgets", "test", "MIT", "trunk", False))
        store.ensure_sync_state(REPO_ID)
        store.ensure_sync_state(REPO_ID)  # second call must not fail or reset
        store.set_cursor(REPO_ID, "c1")
        store.ensure_sync_state(REPO_ID)
        assert store.get_sync_state(REPO_ID).backfill_cursor == "c1"  # type: ignore[union-attr]
        store.finish_backfill(REPO_ID, when, when)
        store.finish_backfill(REPO_ID, datetime(2030, 1, 1, tzinfo=when.tzinfo), when)
        state = store.get_sync_state(REPO_ID)
        assert state is not None and state.backfill_cursor is None
        assert state.backfill_done_at == when  # set once, never overwritten

        wf = WorkflowRecord(REPO_ID, ".github/workflows/a.yml", "A", False, None)
        store.upsert_workflow(wf)
        store.upsert_workflow(WorkflowRecord(REPO_ID, wf.path, "A", True, "abc"))
        assert store.get_workflows(REPO_ID)[wf.path].detected_at_sha == "abc"

        run = RunRecord(
            1, 1, REPO_ID, wf.path, "s" * 40, "main", "push", "in_progress", None,
            when, when, when, "h", [3, 4], raw={"k": [1, 2]},
        )  # fmt: skip
        store.upsert_run(run)
        assert not store.attempt_done(REPO_ID, 1, 1)
        store.upsert_run(
            RunRecord(**{**run.__dict__, "status": "completed", "conclusion": "success"})
        )
        assert store.attempt_done(REPO_ID, 1, 1) and not store.attempt_done(REPO_ID, 1, 2)
        assert not store.attempt_done(REPO_ID + 1, 1, 1)

        store.upsert_job(
            JobRecord(7, 1, 1, "t", "t", "completed", "success", when, when, "Linux", ["a"])
        )
        store.upsert_job(JobRecord(7, 1, 1, "t2", None, "completed", "failure", when, when, None))
        step = StepRecord(7, 1, "s", "completed", "success", when, when)
        store.upsert_steps([step, StepRecord(7, 2, "s2", None, None, None, None)])
        store.upsert_steps([StepRecord(7, 1, "renamed", "completed", "success", when, when)])

        commit = CommitRecord(REPO_ID, "c" * 40, ["p" * 40], "h", when, "msg")
        files = [
            CommitFileRecord(REPO_ID, commit.sha, "b.js", None, "modified", 1, 0, "p", False),
            CommitFileRecord(REPO_ID, commit.sha, "a.js", "old.js", "renamed", 0, 0, None, True),
        ]
        assert not store.has_commit(REPO_ID, commit.sha)
        store.upsert_commit(commit, files)
        store.upsert_commit(commit, files)
        assert store.has_commit(REPO_ID, commit.sha)
        assert store.commit_files(REPO_ID, commit.sha) == [
            ("a.js", "renamed"),
            ("b.js", "modified"),
        ]

        snap = SnapshotRecord(REPO_ID, commit.sha, "package.json", "{}", None, when)
        assert store.get_snapshot(REPO_ID, commit.sha, "package.json") is None
        store.upsert_snapshot(snap)
        store.upsert_snapshot(
            SnapshotRecord(REPO_ID, commit.sha, "package.json", None, "ref", when)
        )
        assert store.get_snapshot(REPO_ID, commit.sha, "package.json").storage_path == "ref"  # type: ignore[union-attr]

        assert not store.has_log(7)
        store.upsert_log(LogRecord(7, None, None, None, None, "missing", when))
        store.upsert_log(LogRecord(7, "logs/x.gz", 10, "0" * 64, 2, "ok", when))
        assert store.has_log(7)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT default_branch, enabled FROM raw.repos")).one() == (
            "trunk",
            False,
        )
        assert conn.execute(text("SELECT name, conclusion FROM raw.jobs")).one() == (
            "t2",
            "failure",
        )
        assert conn.execute(text("SELECT name FROM raw.steps WHERE number = 1")).scalar_one() == (
            "renamed"
        )
        assert conn.execute(text("SELECT pr_numbers FROM raw.workflow_runs")).scalar_one() == [3, 4]
        assert conn.execute(text("SELECT status, bytes FROM raw.logs")).one() == ("ok", 10)


def test_sql_store_rolls_back_a_failed_transaction(engine: Engine) -> None:
    store = SqlStore(engine)
    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction():
            store.upsert_repo(RepoRecord(REPO_ID, "acme/widgets", "test", "MIT", "main"))
            raise RuntimeError("boom")
    assert counts(engine)["repos"] == 0
    with pytest.raises(RuntimeError, match="outside transaction"):
        store.has_log(1)
