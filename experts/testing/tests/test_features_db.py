"""Features against Postgres: inserts a fake repo inside a transaction that is always rolled back."""
# ruff: noqa: E501

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from testing_expert.features import compute_features, compute_features_batch

pytestmark = pytest.mark.integration

REPO = 987_654_321
T0 = datetime(2030, 1, 1, tzinfo=UTC)  # far from real data
JOB_COLS = (
    "(job_id, run_id, attempt, name, job_key, conclusion, started_at, completed_at, runner_os)"
)


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as c:
        c.execute(
            "insert into raw.repos (id, full_name, default_branch) values (%s, 'x/y', 'main')",
            (REPO,),
        )
        yield c
        c.rollback()


def add_run(
    c: psycopg.Connection[Any], n: int, conclusion: str, attempt: int = 1, done_days: float = 0
) -> tuple[int, int, int]:
    """Run n happens on day n, job finishes 10 minutes later. Returns the job key."""
    created = T0 + timedelta(days=n)
    run_id = REPO * 10 + n
    c.execute(
        "insert into raw.workflow_runs (run_id, attempt, repo_id, workflow_path, head_sha, head_branch, event, created_at)"
        " values (%s, %s, %s, 'ci.yml', %s, 'main', 'push', %s) on conflict do nothing",
        (run_id, attempt, REPO, f"sha{n}", created),
    )
    job_id = run_id * 10 + attempt
    c.execute(
        f"insert into raw.jobs {JOB_COLS} values (%s, %s, %s, 'test (20)', 'test', %s, %s, %s, 'Linux')",
        (
            job_id,
            run_id,
            attempt,
            conclusion,
            created,
            created + timedelta(minutes=10, days=done_days),
        ),
    )
    c.execute(
        "insert into raw.commits (repo_id, sha, committed_at) values (%s, %s, %s) on conflict do nothing",
        (REPO, f"sha{n}", created),
    )
    c.execute(
        "insert into raw.commit_files (repo_id, sha, path, additions, deletions) values (%s, %s, 'src/a.ts', 4, 1) on conflict do nothing",
        (REPO, f"sha{n}"),
    )
    return run_id, attempt, job_id


def test_features_do_not_change_when_later_runs_are_added(conn: psycopg.Connection[Any]) -> None:
    keys = [add_run(conn, n, "failure" if n % 3 == 0 else "success") for n in range(1, 9)]
    before = compute_features_batch(conn, keys)
    assert len(before) == 8
    # later runs, including re-runs of earlier runs, must not touch earlier rows
    for n in range(9, 20):
        add_run(conn, n, "failure")
    add_run(conn, 3, "failure", attempt=2, done_days=30)  # re-run finishes much later
    after = compute_features_batch(conn, keys)
    diff = {
        k: {f: (before[k][f], after[k][f]) for f in before[k] if before[k][f] != after[k][f]}
        for k in keys
    }
    assert not any(diff.values()), diff
    # a single-job call agrees with the batch
    assert compute_features(conn, *keys[5]) == before[keys[5]]


def test_history_values_on_fake_repo(conn: psycopg.Connection[Any]) -> None:
    keys = [add_run(conn, n, "failure" if n in (2, 3) else "success") for n in range(1, 6)]
    rows = compute_features_batch(conn, keys)
    first, fourth = rows[keys[0]], rows[keys[3]]
    assert first["h_n_prior"] == 0 and first["h_fail_rate_30d"] is None
    assert fourth["h_n_prior"] == 3 and fourth["h_consecutive_failures"] == 2
    assert fourth["h_fail_rate_7d"] == pytest.approx(2 / 3)
    assert fourth["cs_files_changed"] == 1 and fourth["rs_matrix_size"] == 1
    assert fourth["rs_on_default_branch"] == 1 and fourth["rs_is_pull_request"] == 0


def test_unknown_job_returns_none(conn: psycopg.Connection[Any]) -> None:
    assert compute_features(conn, -1, 1, -1) is None
    assert compute_features_batch(conn, []) == {}
