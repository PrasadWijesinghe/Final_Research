"""P4.9 leakage: triage features may only use data finished at or before the failed job's time.

The labels come from the future (a later re-run passing on the same SHA means flaky, a later fix
commit means genuine), so a feature that read that future would predict the label by cheating.
This test builds a failure, records its features, then adds every kind of later data (the re-run
that passes, later sibling jobs, later test results, mixed outcomes on later commits) and checks
that not one feature moves. A control shows the later data does matter once the cutoff is moved.
"""
# ruff: noqa: E501

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from testing_expert.triage import features, gather, loader

pytestmark = pytest.mark.integration

REPO = 987_654_322
REPO_NAME = "leak/test"
SHA = "a" * 40
T0 = datetime(2031, 1, 1, tzinfo=UTC)  # far from real data
FAIL_AT = T0 + timedelta(hours=10)  # the failed job finishes here
LOG = """\
 FAIL  test/a.test.ts > suite > does a thing
AssertionError: expected 1 to be 2
 ❯ test/a.test.ts:5:10
 ❯ src/lib.ts:52:3

 Test Files  1 failed (1)
      Tests  1 failed | 3 passed (4)
"""
JOB_COLS = (
    "(job_id, run_id, attempt, name, job_key, conclusion, started_at, completed_at, runner_os)"
)


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ["DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(url) as c:
        c.execute(
            "insert into raw.repos (id, full_name, default_branch) values (%s, %s, 'main')",
            (REPO, REPO_NAME),
        )
        yield c
        c.rollback()


def add_job(
    c: psycopg.Connection[Any],
    job_id: int,
    sha: str,
    name: str,
    conclusion: str,
    done: datetime,
    attempt: int = 1,
    run_id: int | None = None,
    workflow: str = "ci.yml",
) -> int:
    run = run_id or job_id
    c.execute(
        "insert into raw.workflow_runs (run_id, attempt, repo_id, workflow_path, head_sha, head_branch,"
        " event, created_at) values (%s, %s, %s, %s, %s, 'main', 'push', %s) on conflict do nothing",
        (run, attempt, REPO, workflow, sha, done - timedelta(minutes=5)),
    )
    c.execute(
        f"insert into raw.jobs {JOB_COLS} values (%s, %s, %s, %s, %s, %s, %s, %s, 'Linux')",
        (job_id, run, attempt, name, name, conclusion, done - timedelta(minutes=5), done),
    )
    return job_id


def add_result(c: psycopg.Connection[Any], job_id: int, test_id: str, outcome: str) -> None:
    c.execute(
        "insert into testing.test_results (job_id, test_id, attempt, file, full_name, outcome,"
        " granularity) values (%s, %s, 1, 'test/a.test.ts', 'suite > does a thing', %s, 'test')",
        (job_id, test_id, outcome),
    )


def build(c: psycopg.Connection[Any]) -> tuple[gather.FailureRef, features.LogEvidence]:
    """The failure and everything that is legitimately in the past."""
    log = features.read_log(LOG, REPO_NAME)
    assert log.is_test_failure and len(log.test_ids) == 1
    tid = log.test_ids[0]
    c.execute(
        "insert into raw.commits (repo_id, sha, committed_at) values (%s, %s, %s)", (REPO, SHA, T0)
    )
    c.execute(
        "insert into raw.commit_files (repo_id, sha, path, additions, deletions, patch)"
        " values (%s, %s, 'src/lib.ts', 3, 1, '@@ -50,2 +50,3 @@')",
        (REPO, SHA),
    )
    # the past: an earlier sibling leg passed, the test had passed and failed on older commits
    add_job(c, 11, SHA, "Tests (18)", "success", T0 + timedelta(hours=9))
    for i, outcome in enumerate(["passed", "failed", "passed", "failed"]):
        j = add_job(c, 20 + i, f"{i:040d}", "Tests (20)", "success", T0 - timedelta(days=4 - i))
        add_result(c, j, tid, outcome)
    failed = add_job(c, 99, SHA, "Tests (20)", "failure", FAIL_AT)
    add_result(c, failed, tid, "failed")  # the failing job's own row is never history
    ref = gather.FailureRef(REPO_NAME, SHA, failed, 1, "Tests (20)", "ci.yml", FAIL_AT)
    return ref, log


def snapshot(
    c: psycopg.Connection[Any], ref: gather.FailureRef, log: features.LogEvidence
) -> dict[str, float]:
    return features.feature_row(gather.gather(c, ref, log, 0.1))


def same(a: dict[str, float], b: dict[str, float]) -> bool:
    return (
        all((math.isnan(a[k]) and math.isnan(b[k])) or a[k] == b[k] for k in a)
        and a.keys() == b.keys()
    )


def test_no_future_data_is_read(conn: psycopg.Connection[Any]) -> None:
    ref, log = build(conn)
    tid = log.test_ids[0]
    before = snapshot(conn, ref, log)
    assert before["ss_family_pass"] == 1 and before["ss_same_job_pass"] == 0
    assert before["th_max_prior_failures"] == 2  # only the two older failures
    assert before["ss_attempt"] == 1

    # --- everything below finishes after the failure; none of it may change any feature ---
    later = FAIL_AT + timedelta(hours=1)
    add_job(
        conn, 100, SHA, "Tests (20)", "success", later, attempt=2, run_id=99
    )  # the re-run passes
    add_job(conn, 101, SHA, "Tests (22)", "success", later + timedelta(hours=1))  # later sibling
    add_job(conn, 102, SHA, "lint", "failure", later + timedelta(hours=2))
    add_job(
        conn, 103, SHA, "Tests (20)", "failure", later + timedelta(hours=3), attempt=3, run_id=99
    )
    for k, outcome in enumerate(["passed", "failed", "passed"]):
        j = add_job(
            conn,
            200 + k,
            SHA,
            "Tests (20)",
            "success",
            later + timedelta(days=k + 1),
            run_id=900 + k,
        )
        add_result(conn, j, tid, outcome)
    add_result(
        conn, 100, tid, "passed"
    )  # same-SHA re-run result: mixed outcomes on this SHA, later
    conn.execute("insert into raw.commits (repo_id, sha) values (%s, %s)", (REPO, "f" * 40))
    conn.execute(
        "insert into raw.commit_files (repo_id, sha, path, additions, deletions) values (%s, %s, 'test/a.test.ts', 1, 1)",
        (REPO, "f" * 40),
    )
    after = snapshot(conn, ref, log)
    assert same(before, after), {
        k: (before[k], after[k]) for k in before if not same({k: before[k]}, {k: after[k]})
    }

    # --- control: with a later cutoff the same rows DO change the features (the test has power) ---
    moved = gather.FailureRef(**{**ref.__dict__, "cutoff": later + timedelta(days=10)})
    control = snapshot(conn, moved, log)
    assert control["ss_same_job_pass"] >= 1 and control["ss_family_pass"] >= 2
    assert control["th_max_prior_failures"] > before["th_max_prior_failures"]
    assert control["th_max_mixed_shas"] > before["th_max_mixed_shas"]


def test_boundaries_sibling_at_cutoff_counts_history_at_cutoff_does_not(
    conn: psycopg.Connection[Any],
) -> None:
    ref, log = build(conn)
    tid = log.test_ids[0]
    base = snapshot(conn, ref, log)
    add_job(conn, 300, SHA, "Tests (20)", "success", FAIL_AT, run_id=300)  # finishes exactly now
    add_result(conn, 300, tid, "passed")  # a result from a job finishing exactly now
    now = snapshot(conn, ref, log)
    assert now["ss_same_job_pass"] == base["ss_same_job_pass"] + 1  # <= cutoff: legitimate
    assert now["th_max_prior_failures"] == base["th_max_prior_failures"]  # < cutoff: excluded
    assert loader.prior_tests(conn, REPO, [tid], 99, FAIL_AT)[0].n_runs == 4


def test_job_info_and_unknown_repo(conn: psycopg.Connection[Any]) -> None:
    ref, log = build(conn)
    info = loader.job_info(conn, ref.job_id)
    assert info is not None and info.completed_at == FAIL_AT and info.head_sha == SHA
    assert loader.job_info(conn, 424242) is None
    unknown = gather.FailureRef("no/such-repo", SHA, 1, 1, "x", "ci.yml", FAIL_AT)
    ev = gather.gather(conn, unknown, log)
    assert ev.same_sha is None and not ev.overlap.commit_files_known  # unknown, not zero
