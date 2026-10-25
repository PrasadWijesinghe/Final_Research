"""Shared checks for `fill-logs --passing failed-runs` (memory store and Postgres).

`seed_scope_scenario` writes a small synthetic repo through the `Store` interface, so the same
checks run against `MemoryStore` and `SqlStore`. The fake GitHub answers 404 for these job ids,
which `fill-logs` records as a "missing" log row: enough to see which logs were requested.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from collector.store import JobRecord, RepoRecord, RunRecord, Store
from recorded_github import REPO, REPO_ID, FakeGitHub
from scenario import ENTRY, make_backfiller

OTHER_REPO_ID = 777001
_BASE = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)

# run id -> list of (attempt, job id, conclusion, status). The run's created_at grows with its id.
_RUNS: dict[int, list[tuple[int, int, str | None, str]]] = {
    # re-run: attempt 1 failed, attempt 2 passed (the flip evidence must stay)
    100: [
        (1, 1001, "failure", "completed"),
        (1, 1002, "success", "completed"),
        (2, 1003, "success", "completed"),
        (2, 1004, "success", "completed"),
    ],
    101: [(1, 1011, "success", "completed"), (1, 1012, "success", "completed")],  # all green
    102: [(1, 1021, "cancelled", "completed"), (1, 1022, "success", "completed")],  # not a failure
    103: [(1, 1031, "timed_out", "completed"), (1, 1032, "success", "completed")],
    104: [(1, 1041, "startup_failure", "completed"), (1, 1042, "success", "completed")],
    105: [(1, 1051, "success", "completed"), (1, 1052, None, "in_progress")],  # not a failure
    # the other way round: attempt 1 passed, attempt 2 failed (still "same run, any attempt")
    106: [(1, 1061, "success", "completed"), (2, 1062, "failure", "completed")],
}
# A run of ANOTHER repo with a failed job must not change this repo's answer.
_OTHER_RUN = (200, [(1, 2001, "failure", "completed"), (1, 2002, "success", "completed")])

# Pending logs, newest run first (then attempt, then job id, all descending).
ALL_PENDING = [
    1062,
    1061,
    1051,
    1042,
    1041,
    1032,
    1031,
    1022,
    1012,
    1011,
    1004,
    1003,
    1002,
    1001,
]
# Same, keeping a passing job only when a job of its run failed (any attempt).
FAILED_RUNS_PENDING = [1062, 1061, 1042, 1041, 1032, 1031, 1004, 1003, 1002, 1001]
LEFT_OUT = [1051, 1022, 1012, 1011]  # passing jobs of runs without a failed job


def seed_scope_scenario(store: Store) -> int:
    """Write the synthetic repos, runs and jobs; return the id of the main repo."""
    with store.transaction():
        store.upsert_repo(RepoRecord(REPO_ID, REPO, "test", "MIT", "main"))
        store.upsert_repo(RepoRecord(OTHER_REPO_ID, "acme/other", "test", "MIT", "main"))
        for repo_id, runs in ((REPO_ID, _RUNS), (OTHER_REPO_ID, dict([_OTHER_RUN]))):
            for run_id, jobs in runs.items():
                created = _BASE + timedelta(hours=run_id - 100)
                for attempt in sorted({j[0] for j in jobs}):
                    store.upsert_run(
                        RunRecord(
                            run_id=run_id,
                            attempt=attempt,
                            repo_id=repo_id,
                            workflow_path=".github/workflows/test.yml",
                            head_sha="e5" * 20,
                            head_branch="main",
                            event="push",
                            status="completed",
                            conclusion="failure",
                            created_at=created,
                            run_started_at=created,
                            updated_at=created,
                            actor_hash=None,
                        )
                    )
                for attempt, job_id, conclusion, status in jobs:
                    store.upsert_job(
                        JobRecord(
                            job_id=job_id,
                            run_id=run_id,
                            attempt=attempt,
                            name="test",
                            job_key="test",
                            status=status,
                            conclusion=conclusion,
                            started_at=created,
                            completed_at=created,
                            runner_os="Linux",
                        )
                    )
    return REPO_ID


def pending_ids(
    store: Store, limit: int | None = None, *, failed_runs_only: bool = False
) -> list[int]:
    """Job ids of `Store.pending_logs` for the main repo, in order."""
    with store.transaction():
        found = store.pending_logs(REPO_ID, limit, failed_runs_only=failed_runs_only)
    return [p.job_id for p in found]


def requested_logs(fake: FakeGitHub) -> list[int]:
    """Job ids whose log the fake GitHub was asked for, in order."""
    return [int(m.group(1)) for r in fake.requests if (m := re.search(r"/jobs/(\d+)/logs$", r))]


def check_default_selection(store: Store) -> None:
    """Without options the pending list is every job the policy wants, newest run first."""
    assert pending_ids(store) == ALL_PENDING
    assert pending_ids(store, failed_runs_only=False) == ALL_PENDING
    assert pending_ids(store, 3) == ALL_PENDING[:3]


def check_failed_runs_selection(store: Store) -> None:
    """Passing jobs count only in runs with a failed job (any attempt); failed jobs always do."""
    selected = pending_ids(store, failed_runs_only=True)
    assert selected == FAILED_RUNS_PENDING  # includes the order, and the other repo's 2002 is out
    assert set(ALL_PENDING) - set(selected) == set(LEFT_OUT)
    # re-run: attempt 2 passed after attempt 1 failed, and the reverse; both keep passing logs
    assert {1003, 1004} <= set(selected) and 1061 in selected
    # cancelled is not a failure, in_progress is not collected, an all-green run is left out
    assert not {1021, 1022, 1052, 1011, 1012, 1051} & set(selected)
    # --limit keeps the newest first
    assert pending_ids(store, 4, failed_runs_only=True) == FAILED_RUNS_PENDING[:4]


def check_fill_scope(store: Store, fake: FakeGitHub, tmp_path: Path) -> None:
    """`fill_logs_repo` with the options requests exactly the selected logs, and resumes."""
    backfiller = make_backfiller(store, tmp_path)
    # pass 1: only the logs of passing jobs in runs with a failed job
    backfiller.fill_logs_repo(ENTRY, passing="failed-runs")
    assert requested_logs(fake) == FAILED_RUNS_PENDING
    # the left-out jobs are still pending, and so are counted by `status`
    assert pending_ids(store) == LEFT_OUT
    with store.transaction():
        assert store.repo_counts(REPO_ID).logs_pending == len(LEFT_OUT)
    # same options again: nothing left to fetch
    fake.requests.clear()
    backfiller.fill_logs_repo(ENTRY, passing="failed-runs")
    assert requested_logs(fake) == []
    # no flag: fills what is still pending, so the end state is complete
    backfiller.fill_logs_repo(ENTRY)
    assert requested_logs(fake) == LEFT_OUT
    assert pending_ids(store) == []


def check_fill_limit(store: Store, fake: FakeGitHub, tmp_path: Path) -> None:
    """--limit with --passing failed-runs takes the newest selected logs, then resumes."""
    backfiller = make_backfiller(store, tmp_path)
    backfiller.fill_logs_repo(ENTRY, 3, passing="failed-runs")
    assert requested_logs(fake) == FAILED_RUNS_PENDING[:3]
    assert pending_ids(store, failed_runs_only=True) == FAILED_RUNS_PENDING[3:]
    backfiller.fill_logs_repo(ENTRY, passing="failed-runs")
    assert requested_logs(fake) == FAILED_RUNS_PENDING  # nothing fetched twice
