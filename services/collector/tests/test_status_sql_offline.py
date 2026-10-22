"""SQL used by `daily` and `status`, checked without a database: it compiles for Postgres and
mentions the expected tables and columns. (The integration tests in `test_daily_db.py` run it.)
"""

from __future__ import annotations

from typing import Any

from collector.store import SqlStore
from recorded_github import NOW
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ClauseElement


class _Result:
    def mappings(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return []

    def one(self) -> tuple[int, int]:
        return (0, 0)

    def scalar_one(self) -> int:
        return 0


class _Conn:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, stmt: ClauseElement) -> _Result:
        self.sql.append(str(stmt.compile(dialect=postgresql.dialect())))
        return _Result()


class _Begin:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Conn:
        return self._conn

    def __exit__(self, *exc: object) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.conn = _Conn()

    def begin(self) -> _Begin:
        return _Begin(self.conn)


def _run() -> list[str]:
    engine = _Engine()
    store = SqlStore(engine)  # type: ignore[arg-type]
    with store.transaction():
        store.set_last_daily_sync(1, NOW)
        store.list_repos("test")
        store.repo_counts(1)
        store.run_days(1)
    return engine.conn.sql


def test_new_statements_compile_for_postgres() -> None:
    sql = _run()
    # update, list, counts (runs, jobs, logs, commits, pending logs), days
    assert len(sql) == 2 + 5 + 1
    update, repos, runs, jobs, logs, commits, pending, days = sql
    assert "LEFT OUTER JOIN raw.logs ON raw.logs.job_id = raw.jobs.job_id" in pending
    assert "raw.logs.job_id IS NULL" in pending and "raw.jobs.status =" in pending
    assert update.startswith("UPDATE raw.sync_state SET last_daily_sync_at=")
    assert "WHERE raw.sync_state.repo_id =" in update
    assert "FROM raw.repos" in repos and "raw.repos.member =" in repos and "ORDER BY" in repos
    assert "count(DISTINCT raw.workflow_runs.run_id)" in runs and "count(*)" in runs
    assert "FROM raw.jobs JOIN raw.workflow_runs ON" in jobs
    assert "raw.jobs.run_id = raw.workflow_runs.run_id" in jobs
    assert "raw.jobs.attempt = raw.workflow_runs.attempt" in jobs
    assert "JOIN raw.jobs ON raw.logs.job_id = raw.jobs.job_id" in logs
    assert "GROUP BY raw.logs.status" in logs
    assert "FROM raw.commits" in commits and "raw.commits.repo_id =" in commits


def test_day_query_is_distinct_utc_dates_of_created_at() -> None:
    days = _run()[-1]
    assert days.startswith("SELECT DISTINCT CAST(timezone('UTC', raw.workflow_runs.created_at)")
    assert "AS DATE) AS day" in days
    assert "raw.workflow_runs.created_at IS NOT NULL" in days
    assert days.rstrip().endswith("ORDER BY day")
