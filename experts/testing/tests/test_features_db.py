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
    c: psycopg.Connection[Any],
    n: int,
    conclusion: str,
    attempt: int = 1,
    done_days: float = 0,
    runner_os: str | None = "Linux",
    day: float | None = None,
) -> tuple[int, int, int]:
    """Run n happens on day n, job finishes 10 minutes later. Returns the job key."""
    created = T0 + timedelta(days=n if day is None else day)
    run_id = REPO * 10 + n
    c.execute(
        "insert into raw.workflow_runs (run_id, attempt, repo_id, workflow_path, head_sha, head_branch, event, created_at)"
        " values (%s, %s, %s, 'ci.yml', %s, 'main', 'push', %s) on conflict do nothing",
        (run_id, attempt, REPO, f"sha{n}", created),
    )
    job_id = run_id * 10 + attempt
    c.execute(
        f"insert into raw.jobs {JOB_COLS} values (%s, %s, %s, 'test (20)', 'test', %s, %s, %s, %s)",
        (
            job_id,
            run_id,
            attempt,
            conclusion,
            created,
            created + timedelta(minutes=10, days=done_days),
            runner_os,
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


def test_history_is_split_by_runner_os(conn: psycopg.Connection[Any]) -> None:
    # same job key on two OSes, interleaved: linux always passes, windows always fails
    lin = [add_run(conn, n, "success", day=n * 2) for n in range(1, 5)]
    win = [
        add_run(conn, n + 50, "failure", runner_os="Windows", day=n * 2 + 1) for n in range(1, 5)
    ]
    last = add_run(conn, 60, "success", day=20)  # linux job after all of them
    last_win = add_run(conn, 61, "failure", runner_os="Windows", day=20)
    rows = compute_features_batch(conn, [*lin, *win, last, last_win])
    r_lin, r_win = rows[last], rows[last_win]
    # job-key history mixes both OSes, the per-OS history does not
    assert r_lin["h_n_prior"] == 8 and r_lin["h_os_n_prior"] == 4
    assert r_lin["h_fail_rate_all"] == pytest.approx(0.5) and r_lin["h_os_fail_rate_all"] == 0
    assert r_win["h_os_n_prior"] == 4 and r_win["h_os_fail_rate_all"] == 1
    assert r_win["h_os_consecutive_failures"] == 4 and r_lin["h_os_consecutive_failures"] == 0
    assert r_lin["h_os_last_failed"] == 0 and r_win["h_os_last_failed"] == 1


def test_os_history_has_no_leakage_and_normalises_unknown_os(
    conn: psycopg.Connection[Any],
) -> None:
    keys = [add_run(conn, n, "failure", day=n) for n in range(1, 4)]
    nul = add_run(conn, 70, "success", runner_os=None, day=10)
    before = compute_features_batch(conn, [*keys, nul])
    for n in range(80, 90):  # later runs on every OS, plus a late re-run of run 2
        add_run(conn, n, "failure", day=20 + n)
        add_run(conn, n + 100, "failure", runner_os="Windows", day=20 + n)
    add_run(conn, 2, "failure", attempt=2, day=2, done_days=60)
    after = compute_features_batch(conn, [*keys, nul])
    assert before == after
    # a job with no runner OS and no label is its own 'unknown' series: no linux history leaks in
    assert before[nul]["h_os_n_prior"] == 0 and before[nul]["h_n_prior"] == 3


def test_test_change_features_from_patch(conn: psycopg.Connection[Any]) -> None:
    key = add_run(conn, 1, "success")
    patch = "@@ -0,0 +1,3 @@\n+it('a', async () => {\n+  setTimeout(f, 5);\n+});"
    conn.execute(
        "insert into raw.commit_files (repo_id, sha, path, additions, deletions, patch)"
        " values (%s, 'sha1', 'test/a.test.js', 3, 0, %s)",
        (REPO, patch),
    )
    conn.execute(  # a test file with no patch makes no difference; a source file is ignored
        "insert into raw.commit_files (repo_id, sha, path, additions, deletions)"
        " values (%s, 'sha1', 'test/big.test.js', 900, 0)",
        (REPO,),
    )
    other = add_run(conn, 2, "success")  # sha2 only has src/a.ts: zeros, not unknown
    nopatch = add_run(conn, 3, "success")
    conn.execute(
        "insert into raw.commit_files (repo_id, sha, path, additions, deletions)"
        " values (%s, 'sha3', 'test/z.test.js', 5, 0)",
        (REPO,),
    )
    rows = compute_features_batch(conn, [key, other, nopatch])
    a = rows[key]
    assert a["tw_new_async_tests"] == 1 and a["tw_set_timeout"] == 1
    assert a["tw_test_files_parsed"] == 1
    assert rows[other]["tw_new_tests"] == 0 and rows[other]["tw_test_files_parsed"] == 0
    assert rows[nopatch]["tw_new_tests"] is None


def add_results(
    c: psycopg.Connection[Any], key: tuple[int, int, int], failed: list[str], passed: list[str]
) -> None:
    """Pretend the log of this job was parsed: one test-level row per test name."""
    run_id, attempt, job_id = key
    c.execute(
        "insert into testing.log_parses (job_id, run_id, attempt, repo, head_sha, job_name, reason,"
        " framework, granularity, parsed_ok, is_test_job) values (%s, %s, %s, 'x/y', 'sha', 'test (20)',"
        " 'all', 'mocha', 'test', true, true)",
        (job_id, run_id, attempt),
    )
    for name, outcome in [*((n, "failed") for n in failed), *((n, "passed") for n in passed)]:
        c.execute(
            "insert into testing.test_results (job_id, test_id, attempt, file, full_name, outcome,"
            " duration_ms, granularity) values (%s, %s, %s, 'test/a.test.js', %s, %s, null, 'test')",
            (job_id, f"x-{name}", attempt, name, outcome),
        )


def test_per_test_history_has_no_leakage(conn: psycopg.Connection[Any]) -> None:
    keys = [add_run(conn, n, "failure" if n % 2 else "success") for n in range(1, 7)]
    for k, n in zip(keys, range(1, 7), strict=True):
        add_results(conn, k, ["t1"] if n % 2 else [], ["t2"] + ([] if n % 2 else ["t1"]))
    before = compute_features_batch(conn, keys)
    last = before[keys[5]]
    assert last["ht_parsed_runs_90d"] == 5 and last["ht_last_run_failed_tests"] == 1
    assert last["ht_failed_tests_90d"] == 1 and last["ht_flaky_tests_90d"] == 1
    assert before[keys[0]]["ht_parsed_runs_90d"] == 0 and before[keys[0]]["ht_test_level"] is None
    # later runs with failures, and a late re-run of run 2, must not touch earlier rows
    for n in range(8, 14):
        late = add_run(conn, n, "failure", day=n)
        add_results(conn, late, ["t1", "t3", "t4"], [])
    rerun = add_run(conn, 2, "failure", attempt=2, day=2, done_days=60)
    add_results(conn, rerun, ["t9"], [])
    after = compute_features_batch(conn, keys)
    assert before == after
    assert compute_features(conn, *keys[5]) == before[keys[5]]  # single-job path agrees
