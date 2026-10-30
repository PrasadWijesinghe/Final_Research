"""Reads what triage needs from Postgres, as of one failure's time. Read-only.

LEAKAGE RULE: every query carries the failed job's `completed_at` as a cutoff. Test history only
takes results from jobs that finished strictly before it; same-SHA evidence only takes jobs that
finished at or before it (a job that finished later, such as the re-run that later proves a failure
was flaky, is never read). The commit's own files are not history: they were known at push time.
`tests/test_triage_leakage.py` proves this by adding future rows and checking nothing changes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from .evidence import PriorTest, SiblingJob

logger = logging.getLogger(__name__)

Conn = psycopg.Connection[Any]


@dataclass(frozen=True)
class JobInfo:
    """The failed job as the database knows it."""

    job_id: int
    repo_id: int
    repo: str
    head_sha: str
    run_id: int
    attempt: int
    name: str | None
    workflow_path: str
    completed_at: datetime


def job_info(conn: Conn, job_id: int) -> JobInfo | None:
    """The failed job's repo, commit, run and finish time, or None when it is not in `raw`."""
    with conn.cursor() as cur:
        cur.execute(
            """
select j.job_id, w.repo_id, r.full_name, w.head_sha, j.run_id, j.attempt,
       coalesce(j.name, j.job_key), coalesce(w.workflow_path, ''), j.completed_at
from raw.jobs j
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id
where j.job_id = %s and j.completed_at is not null""",
            (job_id,),
        )
        row = cur.fetchone()
    return JobInfo(*row) if row else None


def repo_id_of(conn: Conn, full_name: str) -> int | None:
    """`raw.repos.id` of `owner/name`, or None for an unknown repo."""
    with conn.cursor() as cur:
        cur.execute("select id from raw.repos where full_name = %s", (full_name,))
        row = cur.fetchone()
    return int(row[0]) if row else None


def commit_files(conn: Conn, repo_id: int, sha: str) -> dict[str, str | None] | None:
    """Changed paths of the commit with their patches; None when the commit has no file rows."""
    with conn.cursor() as cur:
        cur.execute(
            "select path, patch from raw.commit_files where repo_id = %s and sha = %s",
            (repo_id, sha),
        )
        rows = cur.fetchall()
    return {p: patch for p, patch in rows} if rows else None


def siblings(
    conn: Conn, repo_id: int, sha: str, exclude_job_id: int, cutoff: datetime
) -> list[SiblingJob]:
    """Other jobs on this commit that finished at or before `cutoff` (pass or fail only)."""
    with conn.cursor() as cur:
        cur.execute(
            """
select j.job_id, coalesce(j.name, j.job_key, ''), coalesce(w.workflow_path, ''), j.attempt,
       j.conclusion, extract(epoch from j.completed_at)
from raw.jobs j
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
where w.repo_id = %s and w.head_sha = %s and j.job_id <> %s
  and j.conclusion in ('success', 'failure')
  and j.completed_at is not null and j.completed_at <= %s""",
            (repo_id, sha, exclude_job_id, cutoff),
        )
        return [
            SiblingJob(int(a), b, c, int(d), e, float(f)) for a, b, c, d, e, f in cur.fetchall()
        ]


_PRIOR_SQL = """
with r as (
  select t.test_id, w.head_sha, t.outcome,
         lag(t.outcome) over (partition by t.test_id order by j.completed_at, j.job_id) as prev
  from testing.test_results t
  join raw.jobs j on j.job_id = t.job_id
  join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
  where t.test_id = any(%(ids)s) and t.outcome in ('passed', 'failed')
    and w.repo_id = %(repo)s and j.job_id <> %(self)s and j.completed_at < %(cutoff)s
), m as (
  select test_id, head_sha from r group by test_id, head_sha
  having bool_or(outcome = 'failed') and bool_or(outcome = 'passed')
)
select r.test_id, count(*), count(*) filter (where r.outcome = 'failed'),
       count(*) filter (where r.prev is not null and r.prev <> r.outcome),
       (select count(*) from m where m.test_id = r.test_id)
from r group by r.test_id"""


def prior_tests(
    conn: Conn, repo_id: int, test_ids: Sequence[str], exclude_job_id: int, cutoff: datetime
) -> list[PriorTest]:
    """History of the given tests from jobs that finished strictly before `cutoff`."""
    if not test_ids:
        return []
    with conn.cursor() as cur:
        # DEFAULT: no parallel workers; Postgres in Docker has a 64 MB shared-memory limit that
        # parallel plans of this query can exceed ("No space left on device").
        cur.execute("set local max_parallel_workers_per_gather = 0")
        cur.execute(
            _PRIOR_SQL,
            {"ids": list(test_ids), "repo": repo_id, "self": exclude_job_id, "cutoff": cutoff},
        )
        return [PriorTest(a, int(b), int(c), int(d), int(e)) for a, b, c, d, e in cur.fetchall()]
