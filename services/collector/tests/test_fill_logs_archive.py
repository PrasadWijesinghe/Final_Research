"""`fill-logs` by run-attempt archive (D41): failure outcomes and the CLI (in-memory store)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from archive_scope import (
    BOM,
    archive_requests,
    job_requests,
    log_body,
    make_archive,
    seed_archive_scenario,
    zip_name,
)
from collector import cli
from collector.repo_config import RepoConfig
from memory_store import MemoryStore
from recorded_github import REPO, FakeGitHub
from scenario import ENTRY, make_backfiller, make_client, read_log

from shared.github import GitHubError

FULL_300 = [
    (0, "Build&Test: node-20, ubuntu-latest", log_body(3001, "archive")),
    (1, "Build&Test: node-22, ubuntu-latest", log_body(3002, "archive")),
    (2, "Lint", log_body(3003, "archive")),
    (3, "Deploy", log_body(3004, "archive")),
]


def _statuses(store: MemoryStore) -> dict[int, str]:
    return {row["job_id"]: row["status"] for row in store.dump()["logs"]}


def test_archive_over_the_size_limit_is_not_read_and_falls_back(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archives[300, 1] = make_archive(FULL_300)
    for job_id in (3001, 3002, 3003, 3004):
        fake_github.job_logs[job_id] = log_body(job_id, "per-job")
    filler = make_backfiller(store, tmp_path)
    filler.archive_max_bytes = 100  # the zip is bigger than this
    summary = filler.fill_logs_repo(ENTRY)
    assert archive_requests(fake_github) == [(300, 1)]
    assert job_requests(fake_github) == [3004, 3003, 3002, 3001]
    assert (summary.archive_fallbacks, summary.archives_fetched, summary.logs_ok) == (1, 0, 4)


def test_archive_that_is_not_a_zip_falls_back(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archives[300, 1] = b"<html>not a zip</html>"
    for job_id in (3001, 3002, 3003, 3004):
        fake_github.job_logs[job_id] = log_body(job_id, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert job_requests(fake_github) == [3004, 3003, 3002, 3001]
    assert (summary.archive_fallbacks, summary.archives_fetched, summary.logs_ok) == (1, 0, 4)


def test_a_corrupt_file_in_the_zip_falls_back_for_that_job_only(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    raw = bytearray(make_archive(FULL_300, stored=True))
    at = raw.index(b"archive log of job 3002")
    raw[at] ^= 0xFF  # the CRC of that one file no longer matches
    fake_github.archives[300, 1] = bytes(raw)
    fake_github.job_logs[3002] = log_body(3002, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert job_requests(fake_github) == [3002]  # only the job whose file is bad
    assert (summary.logs_ok, summary.logs_from_archive) == (4, 3)
    path = next(r for r in store.dump()["logs"] if r["job_id"] == 3002)["storage_path"]
    assert read_log(tmp_path, path) == f"{BOM}per-job log of job 3002\n"


def test_archive_network_error_falls_back_and_the_jobs_own_requests_decide(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archive_connect_error.add((300, 1))
    for job_id in (3001, 3002, 3003, 3004):
        fake_github.job_logs[job_id] = log_body(job_id, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert job_requests(fake_github) == [3004, 3003, 3002, 3001]
    assert (summary.archives_failed, summary.archives_fetched, summary.logs_ok) == (1, 0, 4)
    # a network error on a job's own request still stops the repo, as before
    store2 = MemoryStore()
    seed_archive_scenario(store2, [300])
    fake_github.log_connect_error.add(3004)
    with pytest.raises(GitHubError, match="transport error"):
        make_backfiller(store2, tmp_path / "two").fill_logs_repo(ENTRY)
    assert _statuses(store2) == {}


def test_archive_5xx_counts_toward_the_guard_but_the_jobs_still_get_their_requests(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archive_status[300, 1] = 503
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert (summary.archives_failed, summary.logs_missing) == (1, 4)  # own requests: 404 each
    assert job_requests(fake_github) == [3004, 3003, 3002, 3001]


def test_other_4xx_on_the_archive_lets_each_job_decide(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archive_status[300, 1] = 422  # some other client error
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert job_requests(fake_github) == [3004, 3003, 3002, 3001]
    assert summary.archive_fallbacks == 1 and set(_statuses(store).values()) == {"missing"}


@pytest.mark.parametrize("status", [401, 403, 429])
def test_401_403_and_429_on_the_archive_stop_the_repo(
    fake_github: FakeGitHub, tmp_path: Path, status: int
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archive_status[300, 1] = status
    with pytest.raises(GitHubError) as stopped:
        make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert stopped.value.status_code == status
    assert _statuses(store) == {} and job_requests(fake_github) == []  # a re-run resumes


def test_a_streak_of_failed_archives_stops_the_repo_but_a_success_resets_it(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300, 301, 304])  # newest first: 304, 301, 300
    fake_github.archive_status[304, 1] = 502
    fake_github.archive_status[300, 1] = 502
    fake_github.archives[301, 1] = make_archive([(0, "other", log_body(3013, "archive"))])
    filler = make_backfiller(store, tmp_path)
    filler.max_archive_failures = 2
    summary = filler.fill_logs_repo(ENTRY)  # 502, success (resets), 502: no streak of 2
    assert (summary.archives_failed, summary.archives_fetched) == (2, 1)
    # two failures in a row: the repo stops, with no request for the third attempt
    fake_github.archive_status[301, 1] = 502
    fake_github.requests.clear()
    store2 = MemoryStore()
    seed_archive_scenario(store2, [300, 301, 304])
    stuck = make_backfiller(store2, tmp_path / "two")
    stuck.max_archive_failures = 2
    with pytest.raises(GitHubError, match="looks unhealthy"):
        stuck.fill_logs_repo(ENTRY)
    assert archive_requests(fake_github) == [(304, 1), (301, 1)]
    # the first failed archive still got its jobs' own requests (404 -> missing); the second none
    assert set(_statuses(store2)) == {3041, 3042, 3043}


def test_a_tripped_guard_fails_fast_for_the_next_repo_until_an_archive_works(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [301, 304])  # newest first: 304, 301
    fake_github.archive_status[304, 1] = 502
    fake_github.archive_status[301, 1] = 502
    filler = make_backfiller(store, tmp_path)
    filler.max_archive_failures = 2
    with pytest.raises(GitHubError, match="looks unhealthy"):
        filler.fill_logs_repo(ENTRY)  # the first repo of a command trips the guard
    fake_github.requests.clear()
    stored_before = _statuses(store)  # the failed archive of 304 gave its jobs their own requests
    with pytest.raises(GitHubError, match="looks unhealthy"):
        filler.fill_logs_repo(ENTRY)  # the next repo: its first failed archive stops it at once
    assert archive_requests(fake_github) == [(301, 1)] and job_requests(fake_github) == []
    assert _statuses(store) == stored_before
    # GitHub recovers: the first good archive resets the count
    fake_github.archive_status.pop((301, 1))
    fake_github.archives[301, 1] = make_archive([(0, "other", log_body(3013, "archive"))])
    summary = filler.fill_logs_repo(ENTRY)
    assert (summary.archives_fetched, summary.archives_failed) == (1, 0)


def _utf8_flagged_zip_with_invalid_name() -> bytes:
    """A zip whose entry name is flagged UTF-8 but holds bytes that are not valid UTF-8."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("0_\u00e9\u00e9.txt", b"content")  # a non-ASCII name: zipfile sets the flag
    raw = out.getvalue()
    assert raw.count("\u00e9\u00e9".encode()) == 2  # local header and central directory
    return raw.replace("\u00e9\u00e9".encode(), b"\xff\xfe\xfd\xfc")


def test_zip_with_invalid_utf8_names_falls_back_per_job(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    with pytest.raises(UnicodeDecodeError):  # what zipfile does with it: a ValueError
        zipfile.ZipFile(io.BytesIO(_utf8_flagged_zip_with_invalid_name()))
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archives[300, 1] = _utf8_flagged_zip_with_invalid_name()
    for job_id in (3001, 3002, 3003, 3004):
        fake_github.job_logs[job_id] = log_body(job_id, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert (summary.archive_fallbacks, summary.archives_fetched, summary.logs_ok) == (1, 0, 4)
    assert job_requests(fake_github) == [3004, 3003, 3002, 3001]


def _zip_with_an_encrypted_lint_entry() -> bytes:
    """The FULL_300 zip with the Lint entry's `encrypted` flag set in both of its headers."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for index, name, content in FULL_300:
            zf.writestr(f"{index}_{zip_name(name)}.txt", content)
    raw = bytearray(out.getvalue())
    lint = b"2_Lint.txt"
    local = raw.index(lint) - 30  # a local file header is 30 bytes before its name
    central = raw.index(lint, raw.index(lint) + 1) - 46  # a central directory entry, 46 bytes
    assert raw[local : local + 4] == b"PK\x03\x04" and raw[central : central + 4] == b"PK\x01\x02"
    raw[local + 6] |= 0x1
    raw[central + 8] |= 0x1
    return bytes(raw)


def test_an_encrypted_entry_falls_back_for_that_job_only(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError):  # what zipfile does with it
        zipfile.ZipFile(io.BytesIO(_zip_with_an_encrypted_lint_entry())).read("2_Lint.txt")
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archives[300, 1] = _zip_with_an_encrypted_lint_entry()
    fake_github.job_logs[3003] = log_body(3003, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert job_requests(fake_github) == [3003]  # only the encrypted one
    assert (summary.logs_ok, summary.logs_from_archive) == (4, 3)


def test_zip_needing_a_newer_version_falls_back_per_job(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    raw = bytearray(make_archive(FULL_300))
    central = raw.index(b"PK\x01\x02")
    raw[central + 6 : central + 8] = b"\xff\x00"  # "version needed to extract": 25.5
    with pytest.raises(NotImplementedError):  # what zipfile does with it
        zipfile.ZipFile(io.BytesIO(bytes(raw)))
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archives[300, 1] = bytes(raw)
    for job_id in (3001, 3002, 3003, 3004):
        fake_github.job_logs[job_id] = log_body(job_id, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert (summary.archive_fallbacks, summary.archives_fetched, summary.logs_ok) == (1, 0, 4)
    assert job_requests(fake_github) == [3004, 3003, 3002, 3001]


def test_a_bzip2_entry_that_does_not_decode_falls_back_for_that_job_only(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for index, name, content in FULL_300:
            kind = zipfile.ZIP_BZIP2 if name == "Lint" else zipfile.ZIP_DEFLATED
            zf.writestr(f"{index}_{zip_name(name)}.txt", content, compress_type=kind)
    raw = bytearray(out.getvalue())
    start = raw.index(b"BZh")  # the one bzip2 stream: keep its header, ruin the rest
    raw[start + 10 : start + 30] = b"\xff" * 20
    with pytest.raises(OSError):  # what zipfile does with it
        zipfile.ZipFile(io.BytesIO(bytes(raw))).read("2_Lint.txt")
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archives[300, 1] = bytes(raw)
    fake_github.job_logs[3003] = log_body(3003, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert job_requests(fake_github) == [3003]
    assert (summary.logs_ok, summary.logs_from_archive) == (4, 3)
    # nothing half-saved: every stored row has its file, and the rest are intact
    for row in store.dump()["logs"]:
        assert row["status"] == "ok" and (tmp_path / row["storage_path"]).is_file()


def test_per_job_401_stops_the_repo_instead_of_marking_logs_missing(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.log_status[3004] = 401  # a dead token
    with pytest.raises(GitHubError) as stopped:
        make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, archive=False)
    assert stopped.value.status_code == 401
    assert _statuses(store) == {}  # nothing was recorded as `missing`


def test_fixture_repo_archive_for_the_attempt_with_two_pending_jobs(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = MemoryStore()
    make_backfiller(store, tmp_path, logs="failed").backfill_repo(ENTRY)
    fake_github.requests.clear()
    # run 9001 attempt 2 holds the pending jobs 5003 and 5004, all other attempts hold one
    fake_github.archives[9001, 2] = make_archive(
        [
            (0, "test (18, ubuntu-latest)", b"log of 5003\n"),
            (1, "test (20, ubuntu-latest)", b"log of 5004\n"),
        ]
    )
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake_github) == [(9001, 2)]
    assert job_requests(fake_github) == [5007, 5002, 5008]
    statuses = _statuses(store)
    assert statuses[5003] == statuses[5004] == "ok"  # per job these were `missing` and `expired`
    assert set(statuses.values()) == {"ok"}


# ---- command line -------------------------------------------------------------------------------


def _patch_cli(monkeypatch: pytest.MonkeyPatch, store: MemoryStore) -> None:
    monkeypatch.setattr(cli, "SqlStore", lambda _engine: store)
    monkeypatch.setattr(cli, "get_engine", lambda: None)
    monkeypatch.setattr(cli, "GitHubClient", make_client)
    monkeypatch.setattr(
        cli, "load_member_config", lambda member: RepoConfig(member="test", repos=[ENTRY])
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x@127.0.0.1/x")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-tests-only")


def test_cli_uses_archives_by_default_and_reports_them(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archives[300, 1] = make_archive(FULL_300[:3])
    fake_github.job_logs[3004] = log_body(3004, "per-job")
    _patch_cli(monkeypatch, store)
    argv = ["fill-logs", "--member", "test", "--data-dir", str(tmp_path)]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == (
        f"{REPO}: logs ok 4, expired 0, missing 0; archives fetched 1 (3 logs), "
        "fell back to one request per job 0, archive failures 0\n"
    )
    assert archive_requests(fake_github) == [(300, 1)] and job_requests(fake_github) == [3004]


def test_cli_no_archive_is_one_request_per_job(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [300])
    fake_github.archives[300, 1] = make_archive(FULL_300)
    _patch_cli(monkeypatch, store)
    argv = ["fill-logs", "--member", "test", "--data-dir", str(tmp_path), "--no-archive"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == f"{REPO}: logs ok 0, expired 0, missing 4\n"
    assert archive_requests(fake_github) == []
    assert job_requests(fake_github) == [3004, 3003, 3002, 3001]


def test_cli_archive_with_passing_failed_runs_and_limit(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    seed_archive_scenario(store, [304, 305])
    fake_github.archives[304, 1] = make_archive(
        [
            (0, "failing", log_body(3041, "archive")),
            (1, "passing a", log_body(3042, "archive")),
            (2, "pass b", log_body(3043, "archive")),
        ]
    )
    _patch_cli(monkeypatch, store)
    argv = ["fill-logs", "--member", "test", "--data-dir", str(tmp_path)]
    assert cli.main([*argv, "--passing", "failed-runs", "--limit", "2"]) == 0
    assert "logs ok 2, expired 0, missing 0; archives fetched 1 (2 logs)" in capsys.readouterr().out
    assert cli.main([*argv, "--passing", "failed-runs"]) == 0  # one pending job left: no archive
    assert capsys.readouterr().out == f"{REPO}: logs ok 0, expired 0, missing 1\n"
    assert archive_requests(fake_github) == [(304, 1)]
