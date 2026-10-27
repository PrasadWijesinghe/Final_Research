"""`collector report` against Postgres (integration: skipped when the database is down).

The in-memory scenarios of `test_report.py` run again through `SqlStore`, so the SQL behind the
report is checked on the real tables and must give the same answers as the in-memory store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from collector.report import collect_report, find_problems, render_report
from collector.store import RepoQuality, SqlStore
from memory_store import MemoryStore
from recorded_github import REPO, REPO_ID, FakeGitHub
from scenario import ENTRY, make_backfiller
from sqlalchemy import Engine
from test_backfill_db import dump_db
from test_report import (
    NEVER,
    OTHER_REPO_ID,
    REPORT_TIME,
    add_extra_jobs,
    add_other_repo,
    set_redactions,
    table_rows,
)

pytestmark = pytest.mark.integration


def test_fixture_report_matches_the_in_memory_store(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    sql, memory = SqlStore(engine), MemoryStore()
    make_backfiller(sql, tmp_path / "sql").backfill_repo(ENTRY)
    make_backfiller(memory, tmp_path / "mem").backfill_repo(ENTRY)

    from_sql = collect_report(sql, "test", [ENTRY, NEVER])
    assert from_sql == collect_report(memory, "test", [ENTRY, NEVER])
    text = render_report("test", from_sql, REPORT_TIME)
    assert table_rows(text, "Runs")[REPO] == ["3", "4", "1", "2", "2026-08-01", "2026-10-15", "0"]
    assert table_rows(text, "Jobs")[REPO] == ["8", "5", "2", "0", "1", "0", "0", "2"]
    assert table_rows(text, "Logs")[REPO] == ["5", "1", "1", "0", "1", "14%"]
    assert find_problems(from_sql) == [
        "- Repos with zero runs (1): acme/never-collected (not collected yet)"
    ]


def test_extra_conclusions_redactions_and_a_second_repo_match_memory(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    sql, memory = SqlStore(engine), MemoryStore()
    for store, folder in ((sql, "sql"), (memory, "mem")):
        make_backfiller(store, tmp_path / folder).backfill_repo(ENTRY)
        add_extra_jobs(store)
        set_redactions(store, {5002: 4, 5007: 2})
        add_other_repo(store)

    [widgets] = collect_report(sql, "test", [ENTRY])
    assert [widgets] == collect_report(memory, "test", [ENTRY])
    assert widgets.quality == RepoQuality(
        jobs_by_conclusion={
            "success": 5,
            "failure": 2,
            "cancelled": 1,
            "timed_out": 1,
            "skipped": 1,
            "startup_failure": 1,
            None: 1,
        },  # fmt: skip
        runs_with_failed_job=3,
        runs_with_reruns=1,
        redactions=7,
        first_run_at=widgets.quality.first_run_at,
        last_run_at=widgets.quality.last_run_at,
    )
    assert widgets.counts.logs_pending == 2
    with sql.transaction():
        other = sql.repo_quality(OTHER_REPO_ID)
        assert sql.repo_quality(999) == RepoQuality()  # unknown repo: all zero
    assert other.jobs_by_conclusion == {"failure": 1} and other.redactions == 9
    assert (other.runs_with_failed_job, other.runs_with_reruns) == (1, 1)


def test_skipped_and_pending_match_memory(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    fake_github.jobs_status = {9002: 502}
    sql, memory = SqlStore(engine), MemoryStore()
    make_backfiller(sql, tmp_path / "sql", logs="failed").backfill_repo(ENTRY)
    make_backfiller(memory, tmp_path / "mem", logs="failed").backfill_repo(ENTRY)
    [from_sql] = collect_report(sql, "test", [ENTRY])
    assert [from_sql] == collect_report(memory, "test", [ENTRY])
    assert from_sql.counts.runs_skipped == 1
    assert from_sql.counts.logs_pending == 4  # 5002, 5003, 5004 and 5008 wait for `fill-logs`


def test_report_changes_nothing_in_the_database(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = SqlStore(engine)
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    before = dump_db(engine)
    render_report("test", collect_report(store, "test", [ENTRY, NEVER]), REPORT_TIME)
    assert dump_db(engine) == before
    with store.transaction():
        assert store.repo_quality(REPO_ID).runs_with_reruns == 1
