"""Runs GitHub keeps failing on (5xx) are skipped and retried later; outages still abort."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest
from collector.status import RateLimit, collect_status, render_status
from collector.store import SkippedRunRecord
from memory_store import MemoryStore
from recorded_github import FAKE_TOKEN, NOW, REPO_ID, FakeGitHub
from scenario import ENTRY, check_expected_state, make_backfiller, make_daily

from shared.github import GitHubClient, GitHubConfig, GitHubError

LATER_DAY = NOW + timedelta(days=1)


def _client(sizes: tuple[int, ...] = (30, 10)) -> GitHubClient:
    config = GitHubConfig(max_retries=0, fallback_page_sizes=sizes)
    return GitHubClient(token=FAKE_TOKEN, config=config, sleep=lambda _s: None)


def _skipped(store: MemoryStore) -> list[int]:
    return [r["run_id"] for r in store.dump()["skipped_runs"]]


def test_smaller_pages_after_a_502_give_the_same_jobs(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    reference = MemoryStore()
    make_backfiller(reference, tmp_path / "ref").backfill_repo(ENTRY)

    fake_github.jobs_max_per_page = 2  # 100 per page fails with 502; 2 per page works
    store = MemoryStore()
    client = _client(sizes=(2, 1))
    summary = make_backfiller(store, tmp_path / "small", client).backfill_repo(ENTRY)

    assert summary.runs_skipped_failing == 0 and _skipped(store) == []
    assert store.dump() == reference.dump()  # same jobs, no duplicates, same everything
    assert fake_github.count("/runs/9001/jobs?filter=all&per_page=2&page=2") >= 1


def test_persistent_502_skips_the_run_and_the_repo_finishes(
    fake_github: FakeGitHub, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake_github.jobs_status = {9002: 502}
    store = MemoryStore()
    with caplog.at_level(logging.WARNING):
        summary = make_backfiller(store, tmp_path, _client()).backfill_repo(ENTRY)
    dump = store.dump()
    assert _skipped(store) == [9002]
    assert summary.runs_skipped_failing == 1
    assert {r["run_id"] for r in dump["workflow_runs"]} == {9001, 9004}  # no half-collected run
    assert not {5005, 5006, 5007} & {j["job_id"] for j in dump["jobs"]}
    assert dump["sync_state"][0]["backfill_cursor"] is None  # cursor done
    assert dump["sync_state"][0]["backfill_done_at"] == NOW
    warned = [r for r in caplog.records if "run skipped" in r.getMessage()]
    assert warned and getattr(warned[0], "run_id", None) == 9002
    assert getattr(warned[0], "repo", None) == ENTRY.full_name
    assert fake_github.count("/runs/9002/jobs") == 3  # 100, then 30, then 10 per page


def test_next_backfill_retries_the_skipped_run_and_clears_it(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    fake_github.jobs_status = {9002: 502}
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)

    # still failing: retried once (not twice), still skipped
    fake_github.requests.clear()
    summary = make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    assert _skipped(store) == [9002] and summary.runs_skipped_failing == 1
    assert fake_github.count("/runs/9002/jobs") == 3

    fake_github.jobs_status = {}
    summary = make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    assert _skipped(store) == [] and summary.skipped_cleared == 1
    check_expected_state(store.dump(), tmp_path)


def test_connect_error_still_aborts_and_skips_nothing(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    fake_github.jobs_connect_error = {9002}
    store = MemoryStore()
    with pytest.raises(GitHubError, match="transport error"):
        make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    assert _skipped(store) == []
    assert store.dump()["sync_state"][0]["backfill_done_at"] is None


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_still_abort(fake_github: FakeGitHub, tmp_path: Path, status: int) -> None:
    fake_github.jobs_status = {9002: status}
    store = MemoryStore()
    with pytest.raises(GitHubError):
        make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    assert _skipped(store) == []


def test_daily_retries_skipped_runs(fake_github: FakeGitHub, tmp_path: Path) -> None:
    fake_github.jobs_status = {9002: 502}
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    assert _skipped(store) == [9002]

    fake_github.jobs_status = {}
    summary = make_daily(store, tmp_path, LATER_DAY).daily_repo(ENTRY)
    assert _skipped(store) == [] and summary.skipped_cleared == 1
    assert 9002 in {r["run_id"] for r in store.dump()["workflow_runs"]}


def test_skipped_run_that_no_longer_exists_is_dropped(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    fake_github.jobs_status = {9002: 502}
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    with store.transaction():
        store.add_skipped_run(SkippedRunRecord(REPO_ID, 1234, 1, "gone", NOW))
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    assert _skipped(store) == [9002]  # 1234 is a 404 on GitHub: forgotten


# ---- fill-logs ---------------------------------------------------------------------------


def test_fill_logs_skips_one_failing_log_and_continues(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    fake_github.log_status = {5007: 502}  # the first pending log
    summary = make_backfiller(store, tmp_path, _client()).fill_logs_repo(ENTRY)
    assert summary.logs_failed == 1 and summary.logs_ok == 2
    logged = {r["job_id"] for r in store.dump()["logs"]}
    assert 5007 not in logged and {5008, 5002}.issubset(logged)
    with store.transaction():
        assert store.repo_counts(REPO_ID).logs_pending == 1

    fake_github.log_status = {}
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    check_expected_state(store.dump(), tmp_path)


def test_fill_logs_limit_is_not_used_up_by_failing_logs(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    fake_github.log_status = {5007: 502}
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, limit=2)
    assert (
        summary.logs_failed == 1
        and summary.logs_ok + summary.logs_expired + summary.logs_missing == 2
    )


def test_fill_logs_aborts_on_a_transport_error(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    fake_github.log_connect_error = {5007}
    with pytest.raises(GitHubError, match="transport error"):
        make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)


# ---- status ------------------------------------------------------------------------------


def test_status_shows_the_skipped_run_count(fake_github: FakeGitHub, tmp_path: Path) -> None:
    fake_github.jobs_status = {9002: 502}
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    report = collect_status(store, "test", [ENTRY], NOW.date())
    text = render_status("test", report, RateLimit(unavailable="x"), NOW)
    assert "runs skipped (GitHub server errors): 1" in text
    header = next(line for line in text.splitlines() if line.startswith("repo "))
    assert header.split() == [
        "repo", "runs", "attempts", "jobs", "logs", "ok", "expired", "missing", "commits",
    ]  # fmt: skip
