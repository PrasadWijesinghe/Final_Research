"""Two-pass collection: `backfill --logs failed`, then `fill-logs` (in-memory, fake GitHub)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from collector import cli
from collector.repo_config import RepoConfig, RepoEntry
from collector.status import RateLimit, collect_status, render_status
from collector.store import PendingLog
from memory_store import MemoryStore
from recorded_github import NOW, REPO, FakeGitHub, file_checksums
from scenario import ENTRY, check_expected_state, make_backfiller, make_client

FAILED = {5001, 5005}
PASSING_WITH_LOG = [5002, 5003, 5004, 5007, 5008]  # 5006 is cancelled: never wanted


def _log_requests(fake: FakeGitHub) -> list[int]:
    return [int(m.group(1)) for r in fake.requests if (m := re.search(r"/jobs/(\d+)/logs$", r))]


def _repo_id(store: MemoryStore) -> int:
    return int(next(iter(store.tables["repos"]))[0])


def _pass_one(tmp_path: Path) -> MemoryStore:
    store = MemoryStore()
    make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    return store


def test_failed_only_stores_no_passing_logs_and_no_rows(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    summary = make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    dump = store.dump()
    assert {log["job_id"] for log in dump["logs"]} == FAILED
    assert all(log["status"] == "ok" for log in dump["logs"])
    assert summary.logs_ok == 2 and summary.logs_deferred == 5
    for job_id in PASSING_WITH_LOG:
        assert fake_github.count(f"/jobs/{job_id}/logs") == 0
    on_disk = [p.name for p in (tmp_path / "logs").rglob("*.log.gz")]
    assert sorted(on_disk) == ["5001.log.gz", "5005.log.gz"]
    # everything else is collected as usual
    assert len(dump["workflow_runs"]) == 4 and len(dump["jobs"]) == 8 and len(dump["commits"]) == 3


def test_default_all_is_unchanged(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = MemoryStore()
    summary = make_backfiller(store, tmp_path).backfill_repo(ENTRY)
    check_expected_state(store.dump(), tmp_path)
    assert summary.logs_deferred == 0


def test_invalid_logs_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_backfiller(MemoryStore(), tmp_path, logs="some")


def test_fill_logs_fetches_exactly_the_pending_ones_newest_first(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = _pass_one(tmp_path)
    fake_github.requests.clear()
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    # newest run first (9002, then 9001 attempt 2 by job id, attempt 1, and the August run last)
    assert _log_requests(fake_github) == [5007, 5004, 5003, 5002, 5008]
    assert (summary.logs_ok, summary.logs_expired, summary.logs_missing) == (3, 1, 1)
    # no listing of runs or jobs, no commits, no config files were requested
    for fragment in ("/actions/runs", "/commits/", "raw.githubusercontent.com"):
        assert fake_github.count(fragment) == 0, fragment
    # the end state equals a one-pass `all` backfill, rows and files
    check_expected_state(store.dump(), tmp_path)
    clean_dir = tmp_path / "clean"
    clean = MemoryStore()
    make_backfiller(clean, clean_dir).backfill_repo(ENTRY)
    assert store.dump()["logs"] == clean.dump()["logs"]
    assert file_checksums(tmp_path / "logs") == file_checksums(clean_dir / "logs")


def test_fill_logs_is_idempotent(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = _pass_one(tmp_path)
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    rows, files = store.dump(), file_checksums(tmp_path)
    fake_github.requests.clear()
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert _log_requests(fake_github) == []
    assert (summary.logs_ok, summary.logs_expired, summary.logs_missing) == (0, 0, 0)
    assert store.dump() == rows and file_checksums(tmp_path) == files


def test_fill_logs_limit_takes_the_newest_and_resumes(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = _pass_one(tmp_path)
    fake_github.requests.clear()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, limit=2)
    assert _log_requests(fake_github) == [5007, 5004]
    fake_github.requests.clear()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert _log_requests(fake_github) == [5003, 5002, 5008]


def test_fill_logs_expired_log_gets_a_status_row(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = _pass_one(tmp_path)
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    logs = {row["job_id"]: row for row in store.dump()["logs"]}
    assert logs[5004]["status"] == "expired" and logs[5004]["storage_path"] is None
    assert logs[5003]["status"] == "missing"


def test_fill_logs_for_a_repo_never_collected_does_nothing(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    other = RepoEntry(full_name="acme/never", licence="MIT")
    summary = make_backfiller(MemoryStore(), tmp_path).fill_logs_repo(other)
    assert summary.logs_ok == 0 and _log_requests(fake_github) == []


def test_status_shows_pending_logs(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = _pass_one(tmp_path)

    def report() -> str:
        data = collect_status(store, "test", [ENTRY], NOW.date())
        return render_status("test", data, RateLimit(unavailable="x"), NOW)

    assert store.repo_counts(_repo_id(store)).logs_pending == 5
    assert "logs pending: 5 (run `collector fill-logs`" in report()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert "logs pending: 0\n" in report()


def test_pending_ignores_cancelled_jobs(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = _pass_one(tmp_path)
    with store.transaction():
        pending = store.pending_logs(_repo_id(store), None)
    assert all(isinstance(p, PendingLog) for p in pending)
    assert 5006 not in {p.job_id for p in pending}


def test_fill_logs_cli(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _pass_one(tmp_path)
    monkeypatch.setattr(cli, "SqlStore", lambda _engine: store)
    monkeypatch.setattr(cli, "get_engine", lambda: None)
    monkeypatch.setattr(cli, "GitHubClient", make_client)
    monkeypatch.setattr(
        cli, "load_member_config", lambda member: RepoConfig(member="test", repos=[ENTRY])
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x@127.0.0.1/x")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-tests-only")
    argv = ["fill-logs", "--member", "test", "--data-dir", str(tmp_path)]
    assert cli.main([*argv, "--limit", "2"]) == 0
    assert f"{REPO}: logs ok 1, expired 1, missing 0" in capsys.readouterr().out
    assert cli.main(argv) == 0
    assert f"{REPO}: logs ok 2, expired 0, missing 1" in capsys.readouterr().out
    assert cli.main(["fill-logs", "--member", "test", "--repo", "nobody/nothing"]) == 2
    assert cli.main(["fill-logs", "--member", "test", "--limit", "0"]) == 2


def test_fill_logs_cli_requires_token_and_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("GITHUB_TOKEN", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["fill-logs", "--member", "test"]) == 2
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err and "DATABASE_URL" in err


def test_backfill_logs_argument_is_validated() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["backfill", "--member", "test", "--logs", "bogus"])
    assert exc.value.code == 2
