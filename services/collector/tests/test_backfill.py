"""Backfill against the recorded fake GitHub and an in-memory store (no database needed).

These run in `make check` without Docker. The same scenarios run against Postgres in
`test_backfill_db.py`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from collector.repo_config import RepoEntry
from memory_store import MemoryStore
from recorded_github import (
    FAKE_TOKEN,
    LOG_SECRET,
    REPO,
    FakeGitHub,
    SimulatedCrash,
    file_checksums,
)
from scenario import ENTRY, check_expected_state, make_backfiller


def test_backfill_creates_expected_rows_and_files(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = MemoryStore()
    summary = make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    check_expected_state(store.dump(), tmp_path)
    assert summary.runs_seen == 4
    assert summary.runs_skipped_workflow == 1  # the release workflow
    assert summary.runs_processed == 3
    assert (summary.logs_ok, summary.logs_missing, summary.logs_expired) == (5, 1, 1)
    assert summary.windows_done == summary.windows_total == 13


def test_uses_documented_api_calls(fake_github: FakeGitHub, tmp_path: Path) -> None:
    make_backfiller(MemoryStore(), tmp_path).backfill_repo(ENTRY)
    # re-run metadata only for attempts before the latest one
    assert fake_github.count("/runs/9001/attempts/1") == 1
    assert fake_github.count("/attempts/") == 1
    # one jobs call per run, always with filter=all
    assert fake_github.count("/actions/runs/9001/jobs?") == 1
    assert fake_github.count("filter=all") == 3
    # one commit call per distinct head_sha (the release run is never processed)
    assert fake_github.count("/commits/") == 3
    # the cancelled job (5006) gets no log request; the redirect for 5001 is followed
    assert fake_github.count("/jobs/5006/logs") == 0
    assert fake_github.count("pipelines.actions.githubusercontent.com/log/5001") == 1
    # config files come from raw.githubusercontent.com, not the REST API
    assert fake_github.count("raw.githubusercontent.com") >= 5
    assert fake_github.count("/contents/") == 0


def test_second_run_changes_nothing(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    before_rows = store.dump()
    before_files = file_checksums(tmp_path)
    fake_github.requests.clear()

    summary = make_backfiller(store, tmp_path).backfill_repo(ENTRY)

    assert store.dump() == before_rows
    assert file_checksums(tmp_path) == before_files
    assert summary.runs_processed == 0 and summary.runs_already_done == 3
    # nothing but the repo, workflow list and run listings is requested again
    for fragment in ("/jobs", "/logs", "/commits/", "raw.githubusercontent.com", "/attempts/"):
        assert fake_github.count(fragment) == 0, fragment


def test_gzip_files_are_byte_identical_across_fresh_runs(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    make_backfiller(MemoryStore(), first).backfill_repo(ENTRY)
    make_backfiller(MemoryStore(), second).backfill_repo(ENTRY)
    assert file_checksums(first) == file_checksums(second) != {}


@pytest.mark.parametrize("crash_job", [5005, 5008])
def test_resume_after_crash_reaches_the_same_state(
    fake_github: FakeGitHub, tmp_path: Path, crash_job: int
) -> None:
    clean_store, clean_dir = MemoryStore(), tmp_path / "clean"
    make_backfiller(clean_store, clean_dir).backfill_repo(ENTRY)

    store, data = MemoryStore(), tmp_path / "interrupted"
    fake_github.crash_on_log = {crash_job}
    with pytest.raises(SimulatedCrash):
        make_backfiller(store, data).backfill_repo(ENTRY)
    state = store.dump()["sync_state"][0]
    assert state["backfill_done_at"] is None
    assert (state["backfill_cursor"] is not None) == (crash_job == 5008)
    # the run that crashed was rolled back as a whole: no job without its run row
    run_ids = {r["run_id"] for r in store.dump()["workflow_runs"]}
    assert {j["run_id"] for j in store.dump()["jobs"]} <= run_ids

    fake_github.requests.clear()
    make_backfiller(store, data).backfill_repo(ENTRY)

    assert store.dump() == clean_store.dump()
    assert file_checksums(data) == file_checksums(clean_dir)
    check_expected_state(store.dump(), data)
    if crash_job == 5008:
        # 11 windows were saved before the crash, so only the last two are walked again
        assert fake_github.count("/actions/runs?") == 2


def test_cursor_from_other_settings_is_ignored(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    with store.transaction():
        store.set_cursor(424242, '{"started_at": "2026-10-19T12:00:00+00:00", "days": 30}')
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)  # starts over instead of crashing
    check_expected_state(store.dump(), tmp_path)


def test_explicit_workflows_in_config_replace_detection(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    entry = RepoEntry(full_name=REPO, licence="MIT", workflows=[".github/workflows/release.yml"])
    make_backfiller(store, tmp_path).backfill_repo(entry)
    wfs = {w["path"]: w for w in store.dump()["workflows"]}
    assert wfs[".github/workflows/release.yml"]["relevant"] is True
    assert wfs[".github/workflows/test.yml"]["relevant"] is False
    assert {r["run_id"] for r in store.dump()["workflow_runs"]} == {9003}
    assert fake_github.count("/branches/") == 0  # no detection, so no head sha lookup


def test_nothing_secret_reaches_the_logs(
    fake_github: FakeGitHub, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        make_backfiller(MemoryStore(), tmp_path).backfill_repo(ENTRY)
    for secret in (FAKE_TOKEN, LOG_SECRET, "FAIL test/a.test.js"):
        assert secret not in caplog.text


def test_unreadable_attempt_metadata_skips_only_that_attempt(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    fake_github.hide_attempts = True
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    keys = {(r["run_id"], r["attempt"]) for r in store.dump()["workflow_runs"]}
    assert (9001, 1) not in keys and (9001, 2) in keys
    assert {j["job_id"] for j in store.dump()["jobs"]} >= {5003, 5004}
    assert not {5001, 5002} & {j["job_id"] for j in store.dump()["jobs"]}


def test_secret_inside_a_diff_is_redacted_but_big_patch_rule_uses_raw_size(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    from recorded_github import PATCH_SECRET

    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    files = {(f["sha"], f["path"]): f for f in store.dump()["commit_files"]}
    patch = files[("a1" * 20, "src/a.js")]["patch"]
    assert PATCH_SECRET not in patch and "[REDACTED:" in patch
    assert files[("b2" * 20, "src/big.js")]["patch_truncated"] is True


def test_forbidden_log_is_recorded_missing_and_the_repo_continues(
    fake_github: FakeGitHub, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake_github.log_status = {5002: 403}
    store = MemoryStore()
    with caplog.at_level(logging.WARNING):
        make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    logs = {r["job_id"]: r for r in store.dump()["logs"]}
    assert logs[5002]["status"] == "missing" and logs[5002]["storage_path"] is None
    assert logs[5008]["status"] == "ok"  # later runs were still collected
    assert "log not available" in caplog.text


def test_server_error_on_a_log_still_aborts_for_resume(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    from shared.github import GitHubError

    fake_github.log_status = {5002: 500}
    with pytest.raises(GitHubError):
        make_backfiller(MemoryStore(), tmp_path).backfill_repo(ENTRY)
