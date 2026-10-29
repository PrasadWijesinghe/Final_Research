"""SQL behind `collector report`, checked without a database: only SELECTs, expected tables.

(The integration tests in `test_report_db.py` run it against real tables.)
"""

from __future__ import annotations

from collector.store import SqlStore
from test_status_sql_offline import _Engine


def _quality_sql() -> list[str]:
    engine = _Engine()
    store = SqlStore(engine)  # type: ignore[arg-type]
    with store.transaction():
        store.repo_quality(1)
    return engine.conn.sql


def test_report_statements_are_selects_only() -> None:
    sql = _quality_sql()
    assert len(sql) == 5
    assert all(statement.lstrip().startswith("SELECT") for statement in sql)  # read-only


def test_report_statements_use_the_expected_tables_and_filters() -> None:
    conclusions, failed, reruns, redactions, days = _quality_sql()
    assert "GROUP BY raw.jobs.conclusion" in conclusions
    assert "FROM raw.jobs JOIN raw.workflow_runs ON" in conclusions
    assert "raw.workflow_runs.repo_id =" in conclusions
    assert "count(DISTINCT raw.jobs.run_id)" in failed
    assert "raw.jobs.conclusion IN" in failed
    assert "GROUP BY raw.workflow_runs.run_id" in reruns and "count(*) >" in reruns
    assert "sum(raw.logs.redactions)" in redactions and "coalesce" in redactions
    assert "JOIN raw.jobs ON raw.logs.job_id = raw.jobs.job_id" in redactions
    assert "min(raw.workflow_runs.created_at)" in days
    assert "max(raw.workflow_runs.created_at)" in days
