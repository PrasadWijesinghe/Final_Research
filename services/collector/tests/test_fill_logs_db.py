"""Two-pass collection against Postgres (integration: skipped when the DB is down)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from collector.store import SqlStore
from fill_scope import (
    check_default_selection,
    check_failed_runs_selection,
    check_fill_limit,
    check_fill_scope,
    seed_scope_scenario,
)
from memory_store import MemoryStore
from recorded_github import FakeGitHub, file_checksums
from scenario import ENTRY, check_expected_state, make_backfiller
from sqlalchemy import Engine
from test_backfill_db import dump_db

pytestmark = pytest.mark.integration


def _requested(fake: FakeGitHub) -> list[int]:
    return [int(m.group(1)) for r in fake.requests if (m := re.search(r"/jobs/(\d+)/logs$", r))]


def test_two_passes_end_like_one_pass(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = SqlStore(engine)
    make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    first = dump_db(engine)
    assert {log["job_id"] for log in first["logs"]} == {5001, 5005}
    with store.transaction():
        repo_id = store.list_repos("test")[0].id
        assert store.repo_counts(repo_id).logs_pending == 5

    fake_github.requests.clear()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert _requested(fake_github) == [5007, 5004, 5003, 5002, 5008]  # newest run first
    check_expected_state(dump_db(engine), tmp_path)
    with store.transaction():
        assert store.repo_counts(repo_id).logs_pending == 0

    memory = MemoryStore()
    make_backfiller(memory, tmp_path / "mem").backfill_repo(ENTRY)
    assert dump_db(engine)["logs"] == memory.dump()["logs"]
    assert file_checksums(tmp_path / "logs") == file_checksums(tmp_path / "mem" / "logs")


def test_fill_logs_second_run_fetches_nothing(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = SqlStore(engine)
    make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    rows = dump_db(engine)
    fake_github.requests.clear()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert _requested(fake_github) == []
    assert dump_db(engine) == rows


def test_passing_failed_runs_on_the_fixture_repo(
    engine: Engine, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = SqlStore(engine)
    make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    fake_github.requests.clear()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, passing="failed-runs")
    # run 9001 (attempt 1 failed, attempt 2 passed) and run 9002 qualify; run 9004 is all green
    assert _requested(fake_github) == [5007, 5004, 5003, 5002]
    logged = {log["job_id"] for log in dump_db(engine)["logs"]}
    assert logged == {5001, 5005, 5007, 5004, 5003, 5002}
    with store.transaction():
        repo_id = store.list_repos("test")[0].id
        assert [p.job_id for p in store.pending_logs(repo_id, None)] == [5008]
        assert store.repo_counts(repo_id).logs_pending == 1
    fake_github.requests.clear()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert _requested(fake_github) == [5008]
    check_expected_state(dump_db(engine), tmp_path)


def test_scope_default_selection(engine: Engine) -> None:
    store = SqlStore(engine)
    seed_scope_scenario(store)
    check_default_selection(store)


def test_scope_failed_runs_selection(engine: Engine) -> None:
    store = SqlStore(engine)
    seed_scope_scenario(store)
    check_failed_runs_selection(store)


def test_scope_fill_logs(engine: Engine, fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = SqlStore(engine)
    seed_scope_scenario(store)
    check_fill_scope(store, fake_github, tmp_path)


def test_scope_fill_logs_limit(engine: Engine, fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = SqlStore(engine)
    seed_scope_scenario(store)
    check_fill_limit(store, fake_github, tmp_path)
