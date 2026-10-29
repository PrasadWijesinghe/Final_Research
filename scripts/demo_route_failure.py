"""Replay one real, stored failed job through the router so the Test worker diagnoses it (P4.12).

Usage (from the repo root, with the stack up: `docker compose --profile experts up -d testing-api
testing-worker llm-gateway` and the backend API running):

    uv run --env-file .env python scripts/demo_route_failure.py --list            # flaky candidates
    uv run --env-file .env python scripts/demo_route_failure.py --job-id 123456   # route one

It reads the job, its failed steps and its stored log from the database and `DATA_DIR` (never from
GitHub), builds a replay `FailedJob`, routes it with `api.router.route_failed_job` (saved to
`platform.routed_failures`, queued on Redis) and then waits for the worker's diagnosis to show up in
`GET {API_URL}/failures/{failure_id}`. Needs DATABASE_URL and REDIS_URL (from `.env`); the API URL
is `--api-url`, else `API_URL`, else http://127.0.0.1:8000. No secret is printed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from api.failure_queue import redis_from_env
from api.replay import ReplayRun, SqlReplaySource
from api.router import FailedJob, RouteResult, route_failed_job
from api.schemas import FailureView
from api.store import SqlStore, Store
from features.replay import RunRow
from redis import Redis
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from shared.db import models as m
from shared.db.session import get_database_url

logger = logging.getLogger("demo_route_failure")

# DEFAULT: wait up to 60 s for the worker (one job takes about a second; the first call after the
# container starts loads the models), poll once a second.
DEFAULT_TIMEOUT_S = 60.0
POLL_S = 1.0
DEFAULT_API_URL = "http://127.0.0.1:8000"

# Flaky failures with a stored log that have never been routed (so a demo never repeats one).
_CANDIDATES = text(
    """
select j.job_id, r.full_name, j.name, wr.created_at
from testing.labels l
join raw.jobs j on j.job_id = l.job_id
join raw.workflow_runs wr on wr.run_id = j.run_id and wr.attempt = j.attempt
join raw.repos r on r.id = wr.repo_id
join raw.logs lg on lg.job_id = j.job_id and lg.status = 'ok'
where l.is_final and l.sub_label = 'flaky' and j.conclusion = 'failure'
  and not exists (select 1 from platform.routed_failures f where f.job_id = j.job_id)
order by wr.created_at desc
limit :n
"""
)


@dataclass(frozen=True)
class Candidate:
    """One failed, labelled-flaky job that could be replayed."""

    job_id: int
    repo: str
    job_name: str | None
    created_at: str


def list_candidates(engine: Engine, limit: int = 10) -> list[Candidate]:
    """The newest labelled-flaky failed jobs with a stored log that were never routed."""
    with engine.connect() as conn:
        rows = conn.execute(_CANDIDATES, {"n": limit}).all()
    return [Candidate(int(r[0]), r[1], r[2], r[3].isoformat()) for r in rows]


def load_failed_job(engine: Engine, data_dir: Path, job_id: int) -> FailedJob:
    """Build the replay `FailedJob` for a stored job; raises LookupError when it cannot be built."""
    with Session(engine) as s:
        found = s.execute(
            select(m.Job, m.WorkflowRun, m.Repo.full_name)
            .join(
                m.WorkflowRun,
                (m.WorkflowRun.run_id == m.Job.run_id) & (m.WorkflowRun.attempt == m.Job.attempt),
            )
            .join(m.Repo, m.Repo.id == m.WorkflowRun.repo_id)
            .where(m.Job.job_id == job_id)
        ).first()
    if found is None or found[1].created_at is None:
        raise LookupError(f"job {job_id} is not in the database")
    _, run, full_name = found
    created_at = run.created_at
    assert created_at is not None  # checked above; narrows the type for mypy
    item = ReplayRun(
        full_name,
        RunRow(
            run_id=run.run_id,
            attempt=run.attempt,
            head_sha=run.head_sha,
            created_at=created_at,
            workflow_path=run.workflow_path,
            head_branch=run.head_branch,
            actor_hash=run.actor_hash,
            pr_numbers=tuple(run.pr_numbers or ()),
        ),
    )
    jobs = [j for j in SqlReplaySource(engine, data_dir).failed_jobs(item) if j.job_id == job_id]
    if not jobs:
        raise LookupError(
            f"job {job_id} has no failed step or no readable stored log under {data_dir}"
        )
    return jobs[0]


def wait_for_diagnosis(
    api_url: str, failure_id: str, timeout_s: float = DEFAULT_TIMEOUT_S, poll_s: float = POLL_S
) -> FailureView | None:
    """Poll GET /failures/{id} until a diagnosis appears; None when the time runs out."""
    deadline = time.monotonic() + timeout_s
    view: FailureView | None = None
    while True:
        response = httpx.get(f"{api_url.rstrip('/')}/failures/{failure_id}", timeout=5)
        response.raise_for_status()
        view = FailureView.model_validate(response.json())
        if view.diagnoses or time.monotonic() >= deadline:
            return view if view.diagnoses else None
        time.sleep(poll_s)


def summarise(view: FailureView) -> list[str]:
    """Plain lines describing the first diagnosis (verdict, confidence, cases, text source)."""
    d = view.diagnoses[0]
    repo = view.failure.repo
    details: Any = d.details
    lines = [
        f"failure_id      {view.failure.failure_id}",
        f"repo / job      {repo.owner}/{repo.name} / {view.failure.job_name}",
        f"status          {d.status}",
    ]
    if details is not None and getattr(details, "expert", None) == "test":
        lines += [
            f"verdict         {details.verdict} (confidence {details.verdict_confidence:.2f})",
            f"suggest rerun   {details.suggest_rerun}",
            f"failing tests   {len(details.failing_tests)}",
        ]
    lines += [
        f"retrieved cases {len(d.retrieved_cases)}",
        f"evidence lines  {len(d.evidence)}",
        f"llm_model       {d.llm_model}",
        f"root cause      {(d.root_cause or '')[:160]}",
    ]
    return lines


def route_one(job: FailedJob, store: Store, connection: Redis) -> RouteResult:
    """Route and enqueue (saved first, so GET /failures/{id} finds it)."""
    return route_failed_job(job, store, connection)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--list", action="store_true", help="show recent flaky candidates and exit")
    what.add_argument("--job-id", type=int, help="raw.jobs job_id to route")
    parser.add_argument("--limit", type=int, default=10, help="candidates to list (default 10)")
    parser.add_argument("--api-url", default=os.environ.get("API_URL") or DEFAULT_API_URL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--no-wait", action="store_true", help="enqueue only, do not poll the API")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    engine = create_engine(get_database_url())
    if args.list:
        for c in list_candidates(engine, args.limit):
            print(f"{c.job_id}  {c.created_at[:10]}  {c.repo}  {c.job_name}")
        return 0

    # DEFAULT: DATA_DIR unset means ./data (where `make export-data` and the collector keep logs).
    data_dir = Path(os.environ.get("DATA_DIR") or "data")
    try:
        job = load_failed_job(engine, data_dir, args.job_id)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = route_one(job, SqlStore(engine), redis_from_env())
    if result.failure is None:
        print("not routed: no router rule matched this log", file=sys.stderr)
        return 3
    failure = result.failure
    sent = ",".join(e.value for e in failure.sent_to)
    print(f"routed {failure.failure_id} to {sent} (category {failure.predicted_category.value})")
    if args.no_wait:
        return 0
    view = wait_for_diagnosis(args.api_url, str(failure.failure_id), args.timeout)
    if view is None:
        print(
            f"no diagnosis within {args.timeout:g} s; is the testing-worker running?",
            file=sys.stderr,
        )
        return 4
    print("\n".join(summarise(view)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
