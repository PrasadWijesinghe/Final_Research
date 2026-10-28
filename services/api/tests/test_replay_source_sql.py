"""Replay source against Postgres (integration: auto-skipped when the database is unreachable)."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api.replay import SqlReplaySource
from api_db import engine  # noqa: F401
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session

from shared.db import models as m

pytestmark = pytest.mark.integration

REPO_ID = 987_001
RUN_ID = 987_002
JOB_ID = 987_003


def test_sql_source_lists_runs_and_failed_jobs(engine: Engine, tmp_path: Path) -> None:
    created = datetime(2026, 3, 5, 10, tzinfo=UTC)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "j.log.gz").write_bytes(gzip.compress(b"FAIL test/a.test.ts\n"))
    with Session(engine) as s:
        s.add(m.Repo(id=REPO_ID, full_name="replay-test/widgets", member="test"))
        s.flush()
        s.add(
            m.WorkflowRun(
                run_id=RUN_ID,
                attempt=1,
                repo_id=REPO_ID,
                workflow_path=".github/workflows/ci.yml",
                head_sha="a" * 40,
                head_branch="main",
                created_at=created,
                pr_numbers=[],
            )
        )
        s.add(m.Job(job_id=JOB_ID, run_id=RUN_ID, attempt=1, name="test", conclusion="failure"))
        s.flush()
        s.add(m.Step(job_id=JOB_ID, number=3, name="npm test", conclusion="failure"))
        s.add(m.Log(job_id=JOB_ID, storage_path="logs/j.log.gz", status="ok"))
        s.commit()
    try:
        source = SqlReplaySource(engine, tmp_path)
        start, end = datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)
        runs = [r for r in source.runs_between(start, end, "test") if r.run.run_id == RUN_ID]
        assert len(runs) == 1
        assert not [r for r in source.runs_between(start, end, "build") if r.run.run_id == RUN_ID]
        (job,) = source.failed_jobs(runs[0])
        assert job.replay is True
        assert job.failed_steps[0].name == "npm test"
        assert job.log_excerpt == "FAIL test/a.test.ts\n"
    finally:
        with Session(engine) as s:
            s.execute(delete(m.Log).where(m.Log.job_id == JOB_ID))
            s.execute(delete(m.Step).where(m.Step.job_id == JOB_ID))
            s.execute(delete(m.Job).where(m.Job.job_id == JOB_ID))
            s.execute(delete(m.WorkflowRun).where(m.WorkflowRun.run_id == RUN_ID))
            s.execute(delete(m.Repo).where(m.Repo.id == REPO_ID))
            s.commit()
