"""Daily sync and status queries against Postgres (integration: skipped when the DB is down).

These repeat the in-memory scenarios of `test_daily.py` / `test_status.py` through `SqlStore`, so
the SQL used by `daily` and `status` is checked against the real tables.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from collector.status import collect_status
from collector.store import SqlStore
from memory_store import MemoryStore
from recorded_github import NOW, REPO, REPO_ID, FakeGitHub
from scenario import ENTRY, make_backfiller, make_daily
from sqlalchemy import Engine
from test_backfill_db import counts, dump_db

pytestmark = pytest.mark.integration

LATER = NOW + timedelta(days=1)


def _no_times(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "fetched_at"} for r in rows]


def test_new_attempt_of_an_old_run_is_picked_up_by_the_rescan(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = SqlStore(engine)
    fake_github.attempt_cap = {9001: 1}
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    before = counts(engine)
    assert before["workflow_runs"] == 3 and before["jobs"] == 6

    fake_github.attempt_cap = {}
    make_daily(store, tmp_path, LATER).daily_repo(ENTRY)

    after = counts(engine)
    assert after["workflow_runs"] == 4 and after["jobs"] == 8 and after["logs"] == 7
    rows = dump_db(engine)
    assert rows["sync_state"][0]["last_daily_sync_at"] == LATER
    memory = MemoryStore()
    make_backfiller(memory, tmp_path / "mem").backfill_repo(ENTRY)
    for table, expected in memory.dump().items():
        if table != "sync_state":  # fetch times differ: the new logs were fetched a day later
            assert _no_times(rows[table]) == _no_times(expected), table

    make_daily(store, tmp_path, LATER + timedelta(hours=1)).daily_repo(ENTRY)  # idempotent
    assert counts(engine) == after
    assert dump_db(engine)["workflow_runs"] == rows["workflow_runs"]


def test_status_queries_match_the_in_memory_store(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    sql, memory = SqlStore(engine), MemoryStore()
    make_backfiller(sql, tmp_path / "sql").backfill_repo(ENTRY)
    make_backfiller(memory, tmp_path / "mem").backfill_repo(ENTRY)

    with sql.transaction():
        assert [r.full_name for r in sql.list_repos("test")] == [REPO]
        assert sql.list_repos("build") == []
        counts_sql = sql.repo_counts(REPO_ID)
        days_sql = sql.run_days(REPO_ID)
    assert counts_sql == memory.repo_counts(REPO_ID)
    assert (counts_sql.runs, counts_sql.attempts, counts_sql.jobs, counts_sql.commits) == (
        3,
        4,
        8,
        3,
    )
    assert counts_sql.logs == {"ok": 5, "expired": 1, "missing": 1}
    assert days_sql == memory.run_days(REPO_ID)
    assert [d.isoformat() for d in days_sql] == ["2026-08-01", "2026-10-12", "2026-10-15"]

    today = NOW.date()
    assert collect_status(sql, "test", [ENTRY], today) == collect_status(
        memory, "test", [ENTRY], today
    )


def test_status_counts_only_the_given_repo(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = SqlStore(engine)
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    with store.transaction():
        empty = store.repo_counts(999)
        assert (empty.runs, empty.attempts, empty.jobs, empty.commits, empty.logs) == (
            0, 0, 0, 0, {},
        )  # fmt: skip
        assert store.run_days(999) == []


def test_set_last_daily_sync_updates_only_that_column(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = SqlStore(engine)
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    with store.transaction():
        store.set_last_daily_sync(REPO_ID, LATER)
        state = store.get_sync_state(REPO_ID)
    assert state is not None
    assert state.last_daily_sync_at == LATER
    assert state.backfill_done_at == NOW and state.backfill_cursor is None
