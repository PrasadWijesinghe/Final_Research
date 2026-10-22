"""`collector status`: gap detection, rate limit, report text and the CLI wiring."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from collector import cli
from collector.repo_config import RepoConfig, RepoEntry
from collector.status import (
    RateLimit,
    RepoStatus,
    collect_status,
    fetch_rate_limit,
    find_gaps,
    render_status,
)
from collector.store import RepoRecord, SyncStateRecord
from memory_store import MemoryStore
from recorded_github import FAKE_TOKEN, NOW, REPO, REPO_ID, FakeGitHub
from scenario import ENTRY, make_backfiller, make_client

from shared.github import GitHubClient, GitHubConfig, GitHubError

TODAY = NOW.date()


def _d(month: int, day: int) -> date:
    return date(2026, month, day)


def test_find_gaps_only_between_first_and_last_day() -> None:
    days = [_d(10, 1), _d(10, 2), _d(10, 5), _d(10, 6), _d(10, 9)]
    assert find_gaps(days, TODAY) == [_d(10, 3), _d(10, 4), _d(10, 7), _d(10, 8)]
    # quiet days after the last run (or before the first) are not gaps
    assert find_gaps([_d(10, 5)], TODAY) == []
    assert find_gaps([], TODAY) == []


def test_find_gaps_ignores_days_older_than_the_window() -> None:
    # today is 19 Oct, so the 90-day window starts on 22 July; the 1 July run is out of range
    gaps = find_gaps([_d(7, 1), _d(7, 25)], TODAY)
    assert gaps == [_d(7, 22), _d(7, 23), _d(7, 24)]


def test_rate_limit_is_read_from_the_endpoint(fake_github: FakeGitHub) -> None:
    rate = fetch_rate_limit(make_client())
    assert (rate.limit, rate.remaining) == (5000, 4900)
    assert rate.reset == datetime.fromtimestamp(1790000000, UTC)
    assert fake_github.count("/rate_limit") == 1


def test_rate_limit_without_a_client_or_on_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fetch_rate_limit(None).unavailable
    client = GitHubClient(token=FAKE_TOKEN, config=GitHubConfig(max_retries=0))

    def boom() -> dict[str, object]:
        raise GitHubError("down", 500)

    monkeypatch.setattr(client, "get_rate_limit", boom)
    rate = fetch_rate_limit(client)
    assert rate.unavailable and "GitHubError" in rate.unavailable


def _filled_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore()
    make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    return store


def test_report_shows_counts_state_gaps_and_rate_limit(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = _filled_store(tmp_path)
    entries = [ENTRY, RepoEntry(full_name="acme/never-collected", licence="MIT")]
    report = collect_status(store, "test", entries, TODAY)
    text = render_status("test", report, fetch_rate_limit(make_client()), NOW)

    # table row: runs 3, attempts 4, jobs 8, logs ok 5 / expired 1 / missing 1, commits 3
    row = next(line for line in text.splitlines() if line.startswith(REPO))
    assert row.split() == [REPO, "3", "4", "8", "5", "1", "1", "3"]
    assert "backfill: done 2026-10-19 12:00 UTC" in text
    assert "last daily sync: 2026-10-19 12:00 UTC" in text
    assert "runs created from 2026-08-01 to 2026-10-15" in text
    assert "may just be quiet days" in text
    assert "2026-08-02 to 2026-10-11" in text and "2026-10-13 to 2026-10-14" in text
    assert "limit 5000, remaining 4900" in text and "resets 2026-" in text
    assert "acme/never-collected" in text and "not collected yet" in text
    assert FAKE_TOKEN not in text


def test_report_shows_backfill_in_progress_and_never_synced() -> None:
    store = MemoryStore()
    cursor = (
        '{"started_at": "2026-10-19T12:00:00+00:00", "days": 90, "window_days": 7,'
        ' "windows_done": 4}'
    )
    with store.transaction():
        store.upsert_repo(RepoRecord(REPO_ID, REPO, "test", "MIT", "main"))
        store.ensure_sync_state(REPO_ID)
        store.set_cursor(REPO_ID, cursor)
    report = collect_status(store, "test", [ENTRY], TODAY)
    text = render_status("test", report, RateLimit(unavailable="x"), NOW)
    assert "backfill: in progress (4 of 13 windows done)" in text
    assert "last daily sync: never" in text
    assert "GitHub rate limit: unavailable (x)" in text


def test_report_lists_many_gap_ranges_briefly() -> None:
    days = [_d(9, d) for d in range(1, 30, 2)]  # every other day: 14 single-day gaps
    status = RepoStatus(
        REPO, True, known=True, state=SyncStateRecord(REPO_ID), gap_days=find_gaps(days, TODAY)
    )
    status.first_day, status.last_day, status.days_checked = days[0], days[-1], 29
    text = render_status("test", [status], RateLimit(unavailable="x"), NOW)
    assert "... and 6 more ranges" in text


# ---- CLI wiring (no database, no network) ---------------------------------------------------


def _patch_cli(monkeypatch: pytest.MonkeyPatch, store: MemoryStore) -> None:
    monkeypatch.setattr(cli, "SqlStore", lambda _engine: store)
    monkeypatch.setattr(cli, "get_engine", lambda: None)
    monkeypatch.setattr(cli, "GitHubClient", make_client)  # no retries, no real sleeping
    monkeypatch.setattr(
        cli, "load_member_config", lambda member: RepoConfig(member="test", repos=[ENTRY])
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x@127.0.0.1/x")


def test_status_cli_works_without_a_token(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_cli(monkeypatch, _filled_store(tmp_path))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert cli.main(["status", "--member", "test"]) == 0
    out = capsys.readouterr().out
    assert "GitHub rate limit: unavailable (GITHUB_TOKEN is not set)" in out
    assert REPO in out
    assert fake_github.count("/rate_limit") == 0


def test_status_cli_with_a_token_prints_the_limit_but_not_the_token(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_cli(monkeypatch, _filled_store(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
    assert cli.main(["status", "--member", "test"]) == 0
    captured = capsys.readouterr()
    assert "remaining 4900" in captured.out
    assert FAKE_TOKEN not in captured.out + captured.err


def test_status_cli_needs_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["status", "--member", "test"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def _daily_env(monkeypatch: pytest.MonkeyPatch, store: MemoryStore) -> None:
    _patch_cli(monkeypatch, store)
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)


def test_daily_cli_without_backfill_exits_2_and_makes_no_request(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _daily_env(monkeypatch, MemoryStore())
    assert cli.main(["daily", "--member", "test", "--data-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "collector backfill --member test" in err and REPO in err
    assert fake_github.requests == []


def test_daily_cli_runs_after_a_backfill(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _filled_store(tmp_path)
    _daily_env(monkeypatch, store)
    assert cli.main(["daily", "--member", "test", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{REPO}: window ")
    last = store.dump()["sync_state"][0]["last_daily_sync_at"]
    assert last != NOW  # moved to the real start time of this run


def test_daily_cli_exit_1_when_a_repo_fails_and_sync_point_stays(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _filled_store(tmp_path)
    _daily_env(monkeypatch, store)
    fake_github.fail_runs_listing = True
    assert cli.main(["daily", "--member", "test", "--data-dir", str(tmp_path)]) == 1
    assert "sync point not advanced" in capsys.readouterr().err
    assert store.dump()["sync_state"][0]["last_daily_sync_at"] == NOW
