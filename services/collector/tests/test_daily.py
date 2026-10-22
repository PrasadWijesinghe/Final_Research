"""Daily sync against the recorded fake GitHub and an in-memory store (no database needed)."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from collector.backfill import Backfiller
from collector.daily import DailySummary, DailySyncer, NotBackfilledError
from collector.file_tags import load_file_tagger
from collector.workflow_detection import load_detection_rules
from memory_store import MemoryStore
from recorded_github import NOW, REPO_ID, FakeGitHub, file_checksums
from scenario import ENTRY, check_expected_state, make_backfiller, make_client, make_daily

from shared.github import GitHubError

LATER = NOW + timedelta(days=1)


def _without_sync_state(dump: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """The dump without sync_state and without fetch times (they differ by when a row was made)."""
    return {
        t: [{k: v for k, v in row.items() if k != "fetched_at"} for row in rows]
        for t, rows in dump.items()
        if t != "sync_state"
    }


def _backfill_with_old_attempt_only(
    fake_github: FakeGitHub, store: MemoryStore, data: Path
) -> None:
    """Backfill while run 9001 (created 7 days before NOW) has only its first attempt."""
    fake_github.attempt_cap = {9001: 1}
    make_backfiller(store, data).backfill_repo(ENTRY)
    runs = {(r["run_id"], r["attempt"]) for r in store.dump()["workflow_runs"]}
    assert (9001, 1) in runs and (9001, 2) not in runs


def test_rescan_picks_up_a_new_attempt_of_an_old_run(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    clean_store, clean_dir = MemoryStore(), tmp_path / "clean"
    make_backfiller(clean_store, clean_dir).backfill_repo(ENTRY)

    store, data = MemoryStore(), tmp_path / "data"
    _backfill_with_old_attempt_only(fake_github, store, data)
    assert {j["job_id"] for j in store.dump()["jobs"]} >= {5001, 5002}
    assert not {5003, 5004} & {j["job_id"] for j in store.dump()["jobs"]}

    # A day later somebody re-runs run 9001 (created 12 October, before the last sync).
    fake_github.attempt_cap = {}
    fake_github.requests.clear()
    summary = make_daily(store, data, LATER).daily_repo(ENTRY)

    # 9004 (1 August) is outside the 14-day window; 9003 is the release workflow
    assert (summary.runs_seen, summary.runs_processed, summary.runs_already_done) == (3, 1, 1)
    keys = {(r["run_id"], r["attempt"]) for r in store.dump()["workflow_runs"]}
    assert (9001, 2) in keys
    assert {j["job_id"] for j in store.dump()["jobs"]} >= {5003, 5004}
    logs = {r["job_id"]: r["status"] for r in store.dump()["logs"]}
    assert logs[5003] == "missing" and logs[5004] == "expired"  # log policy applied
    # only the changed run was fetched again; finished runs cost no job or log calls
    assert fake_github.count("/runs/9001/jobs") == 1
    assert fake_github.count("/runs/9002/jobs") == 0 and fake_github.count("/runs/9004/jobs") == 0
    # the end state is the same as a backfill that saw both attempts from the start
    assert _without_sync_state(store.dump()) == _without_sync_state(clean_store.dump())
    assert file_checksums(data) == file_checksums(clean_dir)
    check_expected_state(
        {**store.dump(), "sync_state": clean_store.dump()["sync_state"]}, data
    )  # also the full expected state
    assert store.dump()["sync_state"][0]["last_daily_sync_at"] == LATER


def test_rescan_window_starts_14_days_back(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)  # last_daily_sync_at = NOW
    summary = make_daily(store, tmp_path, LATER).daily_repo(ENTRY)
    assert summary.window_start == LATER - timedelta(days=14)
    assert summary.window_end == LATER


def test_running_daily_twice_changes_nothing(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    make_daily(store, tmp_path, LATER).daily_repo(ENTRY)
    rows, files = _without_sync_state(store.dump()), file_checksums(tmp_path)
    fake_github.requests.clear()

    summary = make_daily(store, tmp_path, LATER + timedelta(hours=6)).daily_repo(ENTRY)

    assert summary.runs_processed == 0
    assert _without_sync_state(store.dump()) == rows
    assert file_checksums(tmp_path) == files
    for fragment in ("/jobs", "/logs", "/commits/", "raw.githubusercontent.com", "/attempts/"):
        assert fake_github.count(fragment) == 0, fragment


def test_catch_up_covers_the_whole_gap(fake_github: FakeGitHub, tmp_path: Path) -> None:
    # Backfill only 30 days, so run 9004 (1 August, 79 days old) is missing; the last sync was 85
    # days ago, so the catch-up window must reach back that far and fetch it.
    store = MemoryStore()
    thirty = Backfiller(
        make_client(),
        store,
        member="test",
        data_dir=tmp_path,
        tagger=load_file_tagger(),
        rules=load_detection_rules(),
        days=30,
        clock=lambda: NOW,
    )
    thirty.backfill_repo(ENTRY)
    assert 9004 not in {r["run_id"] for r in store.dump()["workflow_runs"]}
    last = NOW - timedelta(days=85)
    with store.transaction():
        store.set_last_daily_sync(REPO_ID, last)

    summary = make_daily(store, tmp_path, NOW).daily_repo(ENTRY)

    assert summary.window_start == last and summary.gap_days == 85.0
    assert not summary.data_lost
    assert 9004 in {r["run_id"] for r in store.dump()["workflow_runs"]}
    assert store.dump()["sync_state"][0]["last_daily_sync_at"] == NOW


def test_gap_beyond_retention_warns_and_flags_lost_data(
    fake_github: FakeGitHub, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    with store.transaction():
        store.set_last_daily_sync(REPO_ID, NOW - timedelta(days=120))
    with caplog.at_level(logging.WARNING):
        summary = make_daily(store, tmp_path, NOW).daily_repo(ENTRY)
    assert summary.data_lost and summary.gap_days == 120.0
    assert summary.window_start == NOW - timedelta(days=90)  # nothing older can be fetched
    assert "exceeds the retention" in caplog.text
    assert store.dump()["sync_state"][0]["last_daily_sync_at"] == NOW  # the sync still completes


def test_failed_repo_does_not_advance_last_daily_sync(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    fake_github.fail_runs_listing = True
    with pytest.raises(GitHubError):
        make_daily(store, tmp_path, LATER).daily_repo(ENTRY)
    assert store.dump()["sync_state"][0]["last_daily_sync_at"] == NOW

    fake_github.fail_runs_listing = False  # the next run recovers and moves the point forward
    make_daily(store, tmp_path, LATER + timedelta(hours=1)).daily_repo(ENTRY)
    assert store.dump()["sync_state"][0]["last_daily_sync_at"] == LATER + timedelta(hours=1)


def test_crash_in_the_middle_keeps_the_old_sync_point_and_resumes(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    from recorded_github import SimulatedCrash

    store, data = MemoryStore(), tmp_path / "data"
    _backfill_with_old_attempt_only(fake_github, store, data)
    fake_github.attempt_cap = {}
    fake_github.crash_on_log = {5003}
    with pytest.raises(SimulatedCrash):
        make_daily(store, data, LATER).daily_repo(ENTRY)
    assert store.dump()["sync_state"][0]["last_daily_sync_at"] == NOW
    assert (9001, 2) not in {(r["run_id"], r["attempt"]) for r in store.dump()["workflow_runs"]}

    make_daily(store, data, LATER).daily_repo(ENTRY)
    assert (9001, 2) in {(r["run_id"], r["attempt"]) for r in store.dump()["workflow_runs"]}


def test_without_a_backfill_it_says_to_run_backfill_first(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    with pytest.raises(NotBackfilledError, match="run backfill first"):
        make_daily(store, tmp_path, NOW).daily_repo(ENTRY)
    assert store.dump()["workflow_runs"] == []
    assert fake_github.count("/actions/runs?") == 0


def test_daily_syncer_is_a_backfiller_and_summary_is_typed() -> None:
    assert issubclass(DailySyncer, Backfiller)
    assert DailySummary().runs_processed == 0
