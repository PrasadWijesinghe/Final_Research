"""`collector report`: exact numbers for the recorded fake repo, problems, totals and the CLI."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from collector import cli
from collector.repo_config import RepoEntry
from collector.report import (
    RepoReport,
    collect_report,
    find_problems,
    render_report,
    total_row,
)
from collector.store import (
    JobRecord,
    LogRecord,
    RepoCounts,
    RepoQuality,
    RepoRecord,
    RunRecord,
    Store,
)
from memory_store import MemoryStore
from recorded_github import FAKE_TOKEN, NOW, REPO, FakeGitHub
from scenario import ENTRY, make_backfiller
from test_status import _patch_cli

NEVER = RepoEntry(full_name="acme/never-collected", licence="MIT")
RESERVE = RepoEntry(full_name="acme/reserve", licence="MIT", enabled=False)
REPORT_TIME = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)


def filled_store(tmp_path: Path, logs: str = "all") -> MemoryStore:
    """The fake repo after a full backfill (3 runs, 4 attempts, 8 jobs, 7 logs)."""
    store = MemoryStore()
    make_backfiller(store, tmp_path, logs=logs).backfill_repo(ENTRY)
    return store


def add_extra_jobs(store: Store) -> None:
    """Four more jobs on run 9004: timed_out, skipped, startup_failure and still running."""
    extra = [
        (5100, "completed", "timed_out"),
        (5101, "completed", "skipped"),
        (5102, "completed", "startup_failure"),
        (5103, "in_progress", None),
    ]
    with store.transaction():
        for job_id, status, conclusion in extra:
            store.upsert_job(
                JobRecord(
                    job_id, 9004, 1, f"extra {job_id}", None, status, conclusion, None, None, None
                )  # fmt: skip
            )


def set_redactions(store: Store, per_job: dict[int, int]) -> None:
    """Give these jobs an `ok` log with this many redactions."""
    with store.transaction():
        for job_id, n in per_job.items():
            log = LogRecord(job_id, f"logs/x/{job_id}.log.gz", 10, "0" * 64, n, "ok", NOW)
            store.upsert_log(log)


OTHER_REPO_ID = 777


def add_other_repo(store: Store) -> None:
    """A second repo: run 8001 re-run once, a failed job with a log of 9 redactions."""
    when = datetime(2026, 9, 1, tzinfo=UTC)
    with store.transaction():
        store.upsert_repo(RepoRecord(OTHER_REPO_ID, "acme/other", "test", "MIT", "main"))
        for attempt in (1, 2):
            store.upsert_run(
                RunRecord(
                    8001,
                    attempt,
                    OTHER_REPO_ID,
                    None,
                    "e5" * 20,
                    "main",
                    "push",
                    "completed",
                    "failure",
                    when,
                    when,
                    when,
                    None,
                )  # fmt: skip
            )
        store.upsert_job(
            JobRecord(6001, 8001, 1, "t", None, "completed", "failure", None, None, None)
        )
        store.upsert_log(LogRecord(6001, "logs/o/6001.log.gz", 10, "0" * 64, 9, "ok", when))


def table_rows(text: str, section: str) -> dict[str, list[str]]:
    """The rows of one `## section` table: first cell -> the other cells, as text."""
    lines = text.split(f"## {section}\n", 1)[1].strip().split("\n\n")[0].splitlines()
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in lines]
    return {r[0]: r[1:] for r in rows[2:]}  # skip header and rule


def test_fixture_repo_numbers(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = filled_store(tmp_path)
    asked = len(fake_github.requests)  # the backfill above asked GitHub, the report must not
    repos = collect_report(store, "test", [ENTRY, NEVER, RESERVE])
    text = render_report("test", repos, REPORT_TIME)
    assert len(fake_github.requests) == asked

    assert text.startswith("# PipelineIQ data-quality report: member=test\n")
    assert "As of 2026-10-27 09:30 UTC" in text
    zero = ["0", "0", "0", "0", "-", "-", "0"]
    # runs, attempts, re-run runs, runs with failed job, first run, last run, runs skipped
    widgets = ["3", "4", "1", "2", "2026-08-01", "2026-10-15", "0"]
    assert table_rows(text, "Runs") == {
        REPO: widgets,
        "acme/never-collected": zero,
        "acme/reserve (disabled)": zero,
        "TOTAL": widgets,
    }
    # jobs, success, failure, timed_out, cancelled, skipped, other, failed jobs
    jobs = ["8", "5", "2", "0", "1", "0", "0", "2"]
    assert table_rows(text, "Jobs") == {
        REPO: jobs,
        "acme/never-collected": ["0"] * 8,
        "acme/reserve (disabled)": ["0"] * 8,
        "TOTAL": jobs,
    }
    # logs ok, expired, missing, pending, redactions, expired share
    logs = ["5", "1", "1", "0", "1", "14%"]
    assert table_rows(text, "Logs") == {
        REPO: logs,
        "acme/never-collected": ["0", "0", "0", "0", "0", "-"],
        "acme/reserve (disabled)": ["0", "0", "0", "0", "0", "-"],
        "TOTAL": logs,
    }
    # the never-collected repo is the only problem; the disabled reserve is not checked
    assert find_problems(repos) == [
        "- Repos with zero runs (1): acme/never-collected (not collected yet)"
    ]
    assert "acme/reserve" not in text.split("## Runs")[0]
    assert FAKE_TOKEN not in text


def test_a_clean_repo_has_no_problems(fake_github: FakeGitHub, tmp_path: Path) -> None:
    repos = collect_report(filled_store(tmp_path), "test", [ENTRY])
    text = render_report("test", repos, REPORT_TIME)
    assert "## Problems\n\nNo problems found.\n" in text
    assert find_problems(repos) == []


def test_every_job_conclusion_has_its_column(fake_github: FakeGitHub, tmp_path: Path) -> None:
    store = filled_store(tmp_path)
    add_extra_jobs(store)
    set_redactions(store, {5002: 4, 5007: 2})  # 5001 already has 1 and 5005 has 0
    [repo] = collect_report(store, "test", [ENTRY])
    assert repo.counts.jobs == 12
    assert repo.quality.jobs_by_conclusion == {
        "success": 5, "failure": 2, "cancelled": 1,
        "timed_out": 1, "skipped": 1, "startup_failure": 1, None: 1,
    }  # fmt: skip
    assert (repo.failed_jobs, repo.jobs_other) == (4, 2)  # failure x2, timed_out, startup_failure
    assert repo.quality.redactions == 1 + 4 + 2
    assert repo.quality.runs_with_failed_job == 3  # 9004 now has a timed_out job
    assert repo.counts.logs_pending == 2  # the timed_out and the startup_failure job
    text = render_report("test", [repo], REPORT_TIME)
    assert table_rows(text, "Jobs")[REPO] == ["12", "5", "2", "1", "1", "1", "2", "4"]
    assert table_rows(text, "Logs")[REPO] == ["5", "1", "1", "2", "7", "14%"]


def test_skipped_runs_leave_a_partial_repo_and_are_reported(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    fake_github.jobs_status = {9002: 502}  # GitHub keeps answering 502 for run 9002's jobs
    repos = collect_report(filled_store(tmp_path), "test", [ENTRY])
    text = render_report("test", repos, REPORT_TIME)
    assert table_rows(text, "Runs")[REPO] == ["2", "3", "1", "1", "2026-08-01", "2026-10-12", "1"]
    assert table_rows(text, "Jobs")[REPO][:2] == ["5", "4"]
    assert find_problems(repos) == [
        "- Repos with skipped runs (1, 1 runs in all, GitHub server errors; "
        "retried by `collector backfill` or `daily`): acme/widgets 1"
    ]


def test_logs_left_for_fill_logs_are_reported_as_pending(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    repos = collect_report(filled_store(tmp_path, logs="failed"), "test", [ENTRY])
    text = render_report("test", repos, REPORT_TIME)
    # only the two failed jobs got a log; the five passing ones wait for `fill-logs`
    assert table_rows(text, "Logs")[REPO] == ["2", "0", "0", "5", "1", "0%"]
    assert find_problems(repos) == [
        "- Repos with logs still pending (1, 5 logs in all; run `collector fill-logs`): "
        "acme/widgets 5"
    ]


def test_another_repo_in_the_database_does_not_leak_in(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    store = filled_store(tmp_path)
    before = collect_report(store, "test", [ENTRY])
    add_other_repo(store)
    assert collect_report(store, "test", [ENTRY]) == before
    other = RepoEntry(full_name="acme/other", licence="MIT")
    [repo] = collect_report(store, "test", [other])
    assert (repo.counts.runs, repo.counts.attempts, repo.counts.jobs) == (1, 2, 1)
    assert repo.quality.jobs_by_conclusion == {"failure": 1}
    assert (repo.quality.runs_with_failed_job, repo.quality.runs_with_reruns) == (1, 1)
    assert repo.quality.redactions == 9


# ---- problems and totals on made-up numbers ------------------------------------------------


def _repo(
    name: str,
    *,
    runs: int = 5,
    conclusions: dict[str | None, int] | None = None,
    logs: dict[str, int] | None = None,
    pending: int = 0,
    skipped: int = 0,
    enabled: bool = True,
    first: datetime | None = None,
    last: datetime | None = None,
) -> RepoReport:
    conclusions = {"success": 4, "failure": 1} if conclusions is None else conclusions
    return RepoReport(
        name,
        enabled=enabled,
        known=True,
        counts=RepoCounts(
            runs=runs,
            attempts=runs,
            jobs=sum(conclusions.values()),
            logs=logs or {},
            logs_pending=pending,
            runs_skipped=skipped,
        ),
        quality=RepoQuality(
            jobs_by_conclusion=conclusions,
            runs_with_failed_job=runs_failed(conclusions),
            first_run_at=first,
            last_run_at=last,
        ),
    )


def runs_failed(conclusions: dict[str | None, int]) -> int:
    return 1 if conclusions.get("failure") else 0


def test_problem_rules_and_thresholds() -> None:
    repos = [
        _repo("a/empty", runs=0, conclusions={}),
        _repo("a/jobless", runs=2, conclusions={}),  # runs, but jobs were never collected
        _repo("a/quiet", conclusions={"success": 9}),
        _repo("a/mostly-expired", logs={"ok": 1, "expired": 2}),
        _repo("a/half-expired", logs={"ok": 1, "expired": 1}),  # exactly half: not flagged
        _repo("a/pending", pending=3),
        _repo("a/skips", skipped=2),
        _repo("a/healthy", logs={"ok": 4, "expired": 1}),
        _repo("a/reserve", runs=0, conclusions={}, enabled=False),
    ]
    assert find_problems(repos) == [
        "- Repos with zero runs (1): a/empty",
        "- Repos with runs but no jobs (1, jobs were never collected): a/jobless",
        "- Repos with jobs but no failed jobs (1): a/quiet",
        "- Repos where more than 50% of the logs are expired (1): a/mostly-expired (67% of 3)",
        "- Repos with logs still pending (1, 3 logs in all; run `collector fill-logs`): "
        "a/pending 3",
        "- Repos with skipped runs (1, 2 runs in all, GitHub server errors; "
        "retried by `collector backfill` or `daily`): a/skips 2",
    ]


def test_jobs_other_comes_from_the_conclusions_and_is_never_negative() -> None:
    repo = _repo("a/b", conclusions={"success": 5, "startup_failure": 1, None: 2})
    assert repo.jobs_other == 3
    # a concurrent backfill: `counts.jobs` was read before more jobs arrived
    stale = RepoReport(
        "a/c",
        known=True,
        counts=RepoCounts(runs=1, attempts=1, jobs=2),
        quality=RepoQuality(jobs_by_conclusion={"success": 4, "cancelled": 3, "neutral": 1}),
    )
    assert stale.jobs_other == 1
    assert RepoReport("a/d").jobs_other == 0


def test_startup_failure_counts_as_a_failed_job() -> None:
    repo = _repo("a/b", conclusions={"success": 3, "startup_failure": 1})
    assert repo.failed_jobs == 1
    assert find_problems([repo]) == []


def test_total_row_adds_everything_up() -> None:
    early, late = datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 10, 15, tzinfo=UTC)
    one = _repo(
        "a/one",
        runs=3,
        conclusions={"success": 5, "failure": 2, "cancelled": 1},
        logs={"ok": 5, "expired": 1},
        pending=2,
        skipped=1,
        first=early,
        last=datetime(2026, 9, 1, tzinfo=UTC),
    )
    two = _repo(
        "a/two",
        runs=4,
        conclusions={"success": 6, None: 1},
        logs={"ok": 2, "missing": 3},
        first=datetime(2026, 9, 5, tzinfo=UTC),
        last=late,
    )
    none = RepoReport("a/none")  # no data and no dates
    total = total_row([one, two, none])
    assert total.full_name == "TOTAL"
    assert (total.counts.runs, total.counts.jobs) == (7, 15)
    assert total.counts.logs == {"ok": 7, "expired": 1, "missing": 3}
    assert (total.counts.logs_pending, total.counts.runs_skipped) == (2, 1)
    assert total.quality.jobs_by_conclusion == {
        "success": 11, "failure": 2, "cancelled": 1, None: 1,
    }  # fmt: skip
    assert (total.quality.first_run_at, total.quality.last_run_at) == (early, late)
    assert total_row([none]).quality.first_run_at is None


# ---- CLI (no database, no token, no network) -----------------------------------------------


def test_report_cli_prints_the_report_without_a_token(
    fake_github: FakeGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_cli(monkeypatch, filled_store(tmp_path))
    asked = len(fake_github.requests)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def no_client() -> None:
        raise AssertionError("report must not create a GitHub client")

    monkeypatch.setattr(cli, "GitHubClient", no_client)
    assert cli.main(["report", "--member", "test"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# PipelineIQ data-quality report: member=test\n")
    assert table_rows(out, "Runs")[REPO][:4] == ["3", "4", "1", "2"]
    assert len(fake_github.requests) == asked


def test_report_cli_writes_a_file_with_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    fake_github: FakeGitHub,
) -> None:  # fmt: skip
    _patch_cli(monkeypatch, filled_store(tmp_path / "logs"))
    target = tmp_path / "reports" / "quality_test.md"  # the folder does not exist yet
    assert cli.main(["report", "--member", "test", "--out", str(target)]) == 0
    assert capsys.readouterr().out == f"wrote {target}\n"
    written = target.read_text(encoding="utf-8")
    assert written.startswith("# PipelineIQ data-quality report: member=test\n")
    assert table_rows(written, "Logs")[REPO][:4] == ["5", "1", "1", "0"]


def test_report_cli_cannot_write_to_a_folder_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    fake_github: FakeGitHub,
) -> None:  # fmt: skip
    _patch_cli(monkeypatch, filled_store(tmp_path / "logs"))
    assert cli.main(["report", "--member", "test", "--out", str(tmp_path)]) == 1
    assert "cannot write" in capsys.readouterr().err


def test_report_cli_needs_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["report", "--member", "test"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
