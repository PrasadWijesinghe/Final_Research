"""Shared checks for `fill-logs` by run-attempt log archive (D41), for MemoryStore and Postgres.

`seed_archive_scenario` writes small synthetic runs through the `Store` interface. The archives are
zips built in memory with the layout measured on real data: a top-level `{n}_{job name}.txt` per
job (index = position in the run, gaps for jobs that never ran), a `{job name}/system.txt` folder
file per job (ignored), and file-name characters such as `:` stripped from the job name.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from collector.backfill import RepoSummary
from collector.store import JobRecord, LogRecord, RepoRecord, RunRecord, Store
from recorded_github import LOG_SECRET, NOW, REPO, REPO_ID, FakeGitHub, file_checksums
from scenario import ENTRY, make_backfiller, read_log

BOM = chr(0xFEFF)  # a UTF-8 byte order mark opens GitHub logs; it must survive unchanged
_BASE = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)
Dump = Callable[[], dict[str, list[dict[str, Any]]]]

# run id -> (attempt, [(job id, job name, conclusion)]); a higher run id is a newer run.
_RUNS: dict[int, tuple[int, list[tuple[int, str, str]]]] = {
    300: (
        1,
        [
            (3001, "Build&Test: node-20, ubuntu-latest", "success"),  # zip name has no ":"
            (3002, "Build&Test: node-22, ubuntu-latest", "success"),
            (3003, "Lint", "success"),
            (3004, "Deploy", "success"),  # no file in the zip: gets its own request
            (3005, "Build & Test Passed or Skipped", "skipped"),  # never pending, no file
        ],
    ),
    301: (  # two jobs whose names differ only in stripped characters: both fall back
        1,
        [(3011, "test (a)", "success"), (3012, "test: a", "success"), (3013, "other", "success")],
    ),
    302: (  # "x y" already has its log (not pending) but still makes "x-y" ambiguous
        1,
        [(3021, "x-y", "success"), (3022, "x y", "success"), (3023, "other", "success")],
    ),
    303: (1, [(3031, "solo", "success"), (3032, "stored one", "success")]),  # one pending job
    304: (  # a failed job: its passing jobs qualify for --passing failed-runs
        1,
        [(3041, "failing", "failure"), (3042, "passing a", "success"), (3043, "pass b", "success")],
    ),
    305: (1, [(3051, "green a", "success"), (3052, "green b", "success")]),  # all green
    306: (  # same bytes by archive and by request (byte identity)
        1,
        [(3061, "alpha: 1", "success"), (3062, "beta: 2", "success"), (3063, "gamma", "success")],
    ),
}
_STORED = {3022, 3032}  # jobs that get a raw.logs row in the seed


def zip_name(job_name: str) -> str:
    """What GitHub makes of a job name in the archive: file-name characters are removed."""
    return re.sub(r'[/\\:*?"<>|\x00-\x1f]', "", job_name)


def make_archive(
    files: Sequence[tuple[int, str, bytes]], *, stored: bool = False, folders: bool = True
) -> bytes:
    """A zip of `(index, job name, log bytes)` in the measured layout."""
    out = io.BytesIO()
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(out, "w", method) as zf:
        for index, job_name, content in files:
            name = zip_name(job_name)
            zf.writestr(f"{index}_{name}.txt", content)
            if folders:
                zf.writestr(f"{name}/system.txt", b"runner setup\n")
    return out.getvalue()


def seed_archive_scenario(store: Store, runs: Sequence[int]) -> None:
    """Write the repo and the chosen runs (one attempt each) with their jobs."""
    with store.transaction():
        store.upsert_repo(RepoRecord(REPO_ID, REPO, "test", "MIT", "main"))
        for run_id in runs:
            attempt, jobs = _RUNS[run_id]
            created = _BASE + timedelta(hours=run_id - 300)
            store.upsert_run(
                RunRecord(
                    run_id,
                    attempt,
                    REPO_ID,
                    ".github/workflows/test.yml",
                    "e5" * 20,
                    "main",
                    "push",
                    "completed",
                    "failure",
                    created,
                    created,
                    created,
                    None,
                )  # fmt: skip
            )
            for job_id, name, conclusion in jobs:
                store.upsert_job(
                    JobRecord(
                        job_id,
                        run_id,
                        attempt,
                        name,
                        None,
                        "completed",
                        conclusion,
                        created,
                        created,
                        "Linux",
                    )  # fmt: skip
                )
                if job_id in _STORED:
                    store.upsert_log(LogRecord(job_id, "logs/seed", 1, "00", 0, "ok", NOW))


def archive_requests(fake: FakeGitHub) -> list[tuple[int, int]]:
    """(run id, attempt) of every archive request, in order."""
    found = (re.search(r"/runs/(\d+)/attempts/(\d+)/logs$", r) for r in fake.requests)
    return [(int(m.group(1)), int(m.group(2))) for m in found if m]


def job_requests(fake: FakeGitHub) -> list[int]:
    """Job ids of every per-job log request, in order."""
    return [int(m.group(1)) for r in fake.requests if (m := re.search(r"/jobs/(\d+)/logs$", r))]


def _logs(dump: Dump) -> dict[int, dict[str, Any]]:
    return {row["job_id"]: row for row in dump()["logs"]}


def _text(dump: Dump, data_dir: Path, job_id: int) -> str:
    return read_log(data_dir, str(_logs(dump)[job_id]["storage_path"]))


def log_body(job_id: int, how: str) -> bytes:
    return f"{BOM}{how} log of job {job_id}\n".encode()


def check_archive_replaces_per_job_calls(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> RepoSummary:
    """One request for the attempt; a job with no file in the zip still gets its own request."""
    seed_archive_scenario(store, [300])
    fake.archives[300, 1] = make_archive(
        [
            (0, "Build&Test: node-20, ubuntu-latest", log_body(3001, "archive")),
            (1, "Build&Test: node-22, ubuntu-latest", log_body(3002, "archive")),
            (2, "Lint", log_body(3003, "archive")),
        ]  # index 3 is Deploy (no file), index 4 the skipped job (no file)
    )
    fake.job_logs[3004] = log_body(3004, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [(300, 1)]
    assert job_requests(fake) == [3004]  # not one call per job: only the unmatched one
    logs = _logs(dump)
    assert set(logs) == {3001, 3002, 3003, 3004}  # the skipped job never gets a row
    assert {row["status"] for row in logs.values()} == {"ok"}
    assert _text(dump, tmp_path, 3001) == f"{BOM}archive log of job 3001\n"  # ":" was stripped
    assert _text(dump, tmp_path, 3003) == f"{BOM}archive log of job 3003\n"
    assert _text(dump, tmp_path, 3004) == f"{BOM}per-job log of job 3004\n"
    assert (summary.logs_ok, summary.logs_from_archive, summary.archives_fetched) == (4, 3, 1)
    assert summary.archive_fallbacks == summary.archives_failed == 0
    # nothing left, and a second run asks for nothing
    fake.requests.clear()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert fake.requests == []
    return summary


def check_ambiguous_names_fall_back(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """A name shared by two jobs (or two files) is never matched: own request, right log."""
    seed_archive_scenario(store, [301, 302])
    fake.archives[301, 1] = make_archive(  # two jobs share the key `testa`, and so do two files
        [
            (0, "test (a)", log_body(3011, "archive")),
            (1, "test: a", log_body(3012, "archive")),
            (2, "other", log_body(3013, "archive")),
        ]
    )
    # run 302: one file "x y"; jobs "x-y" (pending) and "x y" (stored): the job side is ambiguous
    fake.archives[302, 1] = make_archive(
        [(0, "x y", log_body(3022, "archive")), (1, "other", log_body(3023, "archive"))]
    )
    for job_id in (3011, 3012, 3013, 3021, 3023):
        fake.job_logs[job_id] = log_body(job_id, "per-job")
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [(302, 1), (301, 1)]
    # only "other" in each run came from its archive; every other job used its own request
    assert sorted(job_requests(fake)) == [3011, 3012, 3021]
    assert _text(dump, tmp_path, 3013) == f"{BOM}archive log of job 3013\n"
    assert _text(dump, tmp_path, 3023) == f"{BOM}archive log of job 3023\n"
    for job_id in (3011, 3012, 3021):
        assert _text(dump, tmp_path, job_id) == f"{BOM}per-job log of job {job_id}\n"
    assert _logs(dump)[3022]["storage_path"] == "logs/seed"  # the stored one was left alone


def check_410_expires_the_whole_attempt(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """A 410 archive confirmed by a 410 on the first job expires the attempt (one extra request)."""
    seed_archive_scenario(store, [300])
    fake.archive_status[300, 1] = 410
    fake.log_status[3004] = 410  # the first pending job's own log has expired too
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [(300, 1)] and job_requests(fake) == [3004]
    logs = _logs(dump)
    assert set(logs) == {3001, 3002, 3003, 3004}
    assert all(r["status"] == "expired" and r["storage_path"] is None for r in logs.values())
    assert (summary.logs_expired, summary.logs_ok, summary.archive_fallbacks) == (4, 0, 0)


def check_410_not_confirmed_falls_back_per_job(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """If the first job's own log is fine, the archive's 410 is not believed: one request each."""
    seed_archive_scenario(store, [300])
    fake.archive_status[300, 1] = 410
    for job_id in (3001, 3002, 3003, 3004):
        fake.job_logs[job_id] = log_body(job_id, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [(300, 1)] and job_requests(fake) == [3004, 3003, 3002, 3001]
    assert {r["status"] for r in _logs(dump).values()} == {"ok"}
    assert (summary.logs_ok, summary.logs_expired, summary.archive_fallbacks) == (4, 0, 1)


def check_404_falls_back(store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump) -> None:
    """A 404 archive sends the attempt's jobs to their own requests (normal ok/missing rules)."""
    seed_archive_scenario(store, [300])
    fake.job_logs[3003] = log_body(3003, "per-job")  # the others have no log: 404 -> missing
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [(300, 1)]
    assert job_requests(fake) == [3004, 3003, 3002, 3001]
    statuses = {job_id: r["status"] for job_id, r in _logs(dump).items()}
    assert statuses == {3001: "missing", 3002: "missing", 3003: "ok", 3004: "missing"}
    assert (summary.archive_fallbacks, summary.archives_fetched) == (1, 0)


def check_5xx_falls_back_to_per_job(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """A 5xx archive sends its jobs to their own requests; a per-job 5xx stays pending (D39)."""
    seed_archive_scenario(store, [300, 303])
    fake.archive_status[300, 1] = 502
    for job_id in (3001, 3002, 3004, 3031):
        fake.job_logs[job_id] = log_body(job_id, "per-job")
    fake.log_status[3003] = 502  # this job's own request fails too
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [(300, 1)]
    assert job_requests(fake) == [3031, 3004, 3003, 3002, 3001]  # run 303 is newer
    assert set(_logs(dump)) == {3032, 3031, 3004, 3002, 3001}  # 3003 left pending
    assert (summary.archives_failed, summary.logs_failed, summary.logs_ok) == (1, 1, 4)
    # once GitHub recovers, a re-run fetches the one job that is left (a single job: no archive)
    del fake.log_status[3003]
    fake.job_logs[3003] = log_body(3003, "per-job")
    fake.requests.clear()
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [] and job_requests(fake) == [3003]
    assert set(_logs(dump)) == {3001, 3002, 3003, 3004, 3031, 3032}


def check_410_with_limit_expires_only_the_budget(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """Under --limit the confirming job counts, then only the budget left is marked expired."""
    seed_archive_scenario(store, [300])
    fake.archive_status[300, 1] = 410
    fake.log_status[3004] = 410
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, 2)
    assert set(_logs(dump)) == {3004, 3003} and summary.logs_expired == 2  # newest first
    assert job_requests(fake) == [3004]
    fake.log_status[3002] = 410  # next run: the first pending job is 3002
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)  # the archive says 410 again
    assert set(_logs(dump)) == {3001, 3002, 3003, 3004}
    assert {row["status"] for row in _logs(dump).values()} == {"expired"}


def check_single_pending_job_uses_its_own_request(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """One pending job in an attempt costs one request either way: no archive, no name match."""
    seed_archive_scenario(store, [303])
    fake.archives[303, 1] = make_archive([(0, "solo", log_body(3031, "archive"))])
    fake.job_logs[3031] = log_body(3031, "per-job")
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [] and job_requests(fake) == [3031]
    assert _text(dump, tmp_path, 3031) == f"{BOM}per-job log of job 3031\n"


def check_no_archive_is_one_request_per_job(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """`archive=False` (--no-archive) never asks for an archive."""
    seed_archive_scenario(store, [300])
    fake.archives[300, 1] = make_archive([(0, "Lint", log_body(3003, "archive"))])
    for job_id in (3001, 3002, 3003, 3004):
        fake.job_logs[job_id] = log_body(job_id, "per-job")
    summary = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, archive=False)
    assert archive_requests(fake) == [] and job_requests(fake) == [3004, 3003, 3002, 3001]
    assert summary.archives_fetched == summary.logs_from_archive == 0
    assert _text(dump, tmp_path, 3003) == f"{BOM}per-job log of job 3003\n"


def check_passing_failed_runs_with_archive(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """The D40 filter picks the jobs; the archive only supplies them (green run: no request)."""
    seed_archive_scenario(store, [304, 305])
    fake.archives[304, 1] = make_archive(
        [
            (0, "failing", log_body(3041, "archive")),
            (1, "passing a", log_body(3042, "archive")),
            (2, "pass b", log_body(3043, "archive")),
        ]
    )
    fake.archives[305, 1] = make_archive(
        [(0, "green a", log_body(3051, "archive")), (1, "green b", log_body(3052, "archive"))]
    )
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, passing="failed-runs")
    assert archive_requests(fake) == [(304, 1)] and job_requests(fake) == []
    assert set(_logs(dump)) == {3041, 3042, 3043}
    # the rest can still be added later, without the flag
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [(304, 1), (305, 1)]
    assert {3051, 3052} <= set(_logs(dump))


def check_limit_never_overshoots(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump
) -> None:
    """`--limit` counts logs stored: an archive gives at most what is left, newest first."""
    seed_archive_scenario(store, [300])
    fake.archives[300, 1] = make_archive(
        [
            (0, "Build&Test: node-20, ubuntu-latest", log_body(3001, "archive")),
            (1, "Build&Test: node-22, ubuntu-latest", log_body(3002, "archive")),
            (2, "Lint", log_body(3003, "archive")),
            (3, "Deploy", log_body(3004, "archive")),
        ]
    )
    fake.job_logs[3004] = log_body(3004, "per-job")
    fake.job_logs[3001] = log_body(3001, "per-job")
    # one log left under the limit: a single request beats a whole archive
    one = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, 1)
    assert one.logs_ok == 1 and archive_requests(fake) == [] and job_requests(fake) == [3004]
    # two left: the archive gives exactly two (newest first), the third job stays pending
    two = make_backfiller(store, tmp_path).fill_logs_repo(ENTRY, 2)
    assert (two.logs_ok, two.logs_from_archive) == (2, 2) and archive_requests(fake) == [(300, 1)]
    assert set(_logs(dump)) == {3004, 3003, 3002}
    # no limit: the last pending job is a single one, fetched on its own
    make_backfiller(store, tmp_path).fill_logs_repo(ENTRY)
    assert job_requests(fake) == [3004, 3001] and set(_logs(dump)) == {3001, 3002, 3003, 3004}


def check_byte_identity(
    store: Store, fake: FakeGitHub, tmp_path: Path, dump: Dump, reset_logs: Callable[[], None]
) -> None:
    """The same bytes by archive and by request give the same row and file, secrets cleaned."""
    seed_archive_scenario(store, [306])
    bodies = {
        3061: b"\xef\xbb\xbf2026-10-15T09:00:00.1Z token " + LOG_SECRET.encode() + b"\nok\n",
        3062: "2026-10-15T09:00:01Z éè café\r\nend".encode(),
        3063: b"",
    }
    fake.archives[306, 1] = make_archive(
        [(0, "alpha: 1", bodies[3061]), (1, "beta: 2", bodies[3062]), (2, "gamma", bodies[3063])]
    )
    fake.job_logs.update(bodies)
    via_archive = tmp_path / "archive"
    make_backfiller(store, via_archive).fill_logs_repo(ENTRY)
    assert archive_requests(fake) == [(306, 1)] and job_requests(fake) == []
    rows_archive = dump()["logs"]
    reset_logs()
    via_jobs = tmp_path / "jobs"
    make_backfiller(store, via_jobs).fill_logs_repo(ENTRY, archive=False)
    assert job_requests(fake) == [3063, 3062, 3061]
    rows_jobs = dump()["logs"]
    assert rows_archive == rows_jobs  # status, size, sha256, redactions, path and time
    assert file_checksums(via_archive / "logs") == file_checksums(via_jobs / "logs")
    assert next(r for r in rows_jobs if r["job_id"] == 3061)["redactions"] >= 1
    assert LOG_SECRET not in _text(dump, via_archive, 3061)
