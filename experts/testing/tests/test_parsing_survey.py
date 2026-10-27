"""The survey tool's pure parts: sampling, the report and the miss list (no database needed)."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest
from testing_expert.parsing import ParsedLog, Summary
from testing_expert.parsing.survey import (
    EXIT_USAGE,
    LogRef,
    Outcome,
    format_report,
    main,
    run_survey,
    sample_stratified,
    share_with_names,
    worst_misses,
)

MOCHA_FAIL = (
    "  1 passing\n  1 failing\n\n  1) s\n       t:\n     Error: x\n"
    "      at f (test/a.test.js:1:1)\n"
)
MOCHA_MISS = "  1 passing\n  2 failing\n\n  1) s\n       t:\n     Error: x\n"


def _refs(counts: dict[str, int]) -> list[LogRef]:
    refs: list[LogRef] = []
    job = 0
    for repo, n in counts.items():
        for _ in range(n):
            job += 1
            refs.append(LogRef(job, repo, f"logs/{repo}/{job}.log.gz"))
    return refs


def test_sampling_is_repeatable_and_lets_a_big_repo_take_only_its_share() -> None:
    refs = _refs({"big/repo": 900, "mid/repo": 60, "small/repo": 5})
    first = sample_stratified(refs, 100, seed=42)
    again = sample_stratified(refs, 100, seed=42)
    other = sample_stratified(refs, 100, seed=7)
    assert [r.job_id for r in first] == [r.job_id for r in again]
    assert [r.job_id for r in first] != [r.job_id for r in other]
    counts = {
        repo: sum(1 for r in first if r.repo == repo)
        for repo in ("big/repo", "mid/repo", "small/repo")
    }
    assert counts["small/repo"] == 5  # a small repo gives everything it has
    assert abs(counts["big/repo"] - counts["mid/repo"]) <= 1  # the others split the rest evenly
    assert sum(counts.values()) == 100
    assert len(first) == 100 and len({r.job_id for r in first}) == 100


def test_sampling_everything_when_the_pool_is_smaller_than_n() -> None:
    refs = _refs({"a/a": 3, "b/b": 4})
    assert sorted(r.job_id for r in sample_stratified(refs, 50, seed=1)) == [r.job_id for r in refs]


def test_sampling_an_empty_pool_gives_nothing() -> None:
    assert sample_stratified([], 10, seed=1) == []


def _write_logs(tmp_path: Path) -> tuple[list[LogRef], Path]:
    refs = [
        LogRef(1, "a/a", "logs/a/1.log.gz"),
        LogRef(2, "a/a", "logs/a/2.log.gz"),
        LogRef(3, "b/b", "logs/b/3.log.gz"),
        LogRef(4, "b/b", "logs/b/missing.log.gz"),
        LogRef(5, "c/c", "logs/c/5.log.gz"),
    ]
    bodies = {1: MOCHA_FAIL, 2: MOCHA_MISS, 3: "npm ERR! build failed\n", 5: "  1 passing\n"}
    for ref in refs:
        if ref.job_id in bodies:
            path = tmp_path / ref.storage_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(bodies[ref.job_id].encode()))
    return refs, tmp_path


def test_run_survey_parses_each_log_and_counts_missing_files(tmp_path: Path) -> None:
    refs, data = _write_logs(tmp_path)
    outcomes, missing = run_survey(refs, data)
    assert [o.ref.job_id for o in outcomes] == [1, 2, 3, 5]
    assert [m.job_id for m in missing] == [4]
    assert [o.result.framework for o in outcomes] == ["mocha", "mocha", "other", "mocha"]


def test_share_with_names_and_worst_misses(tmp_path: Path) -> None:
    refs, data = _write_logs(tmp_path)
    outcomes, _missing = run_survey(refs, data)
    with_names, reporting = share_with_names(outcomes)
    assert (with_names, reporting) == (2, 2)  # job 2 reports 2 failing and yields 1 name
    misses = worst_misses(outcomes, 10)
    assert [(o.ref.job_id, why) for o, why in misses] == [
        (2, "found count differs from the summary")
    ]


def test_a_log_that_reports_failures_but_yields_no_name_is_the_worst_miss() -> None:
    def outcome(job_id: int, failed: int, names: int) -> Outcome:
        result = ParsedLog(framework="jest", summary=Summary(failed=failed), parsed_ok=names > 0)
        return Outcome(LogRef(job_id, "r/r", "p"), result, 0.01)

    outcomes = [outcome(1, 5, 0), outcome(2, 50, 0), outcome(3, 0, 0)]
    misses = worst_misses(outcomes, 10)
    assert [o.ref.job_id for o, _why in misses] == [2, 1]  # the larger report first
    assert share_with_names(outcomes) == (0, 2)


def test_the_report_has_the_rates_and_no_log_text(tmp_path: Path) -> None:
    refs, data = _write_logs(tmp_path)
    outcomes, missing = run_survey(refs, data)
    report = format_report(outcomes, missing, total_failed=15553, worst=10)
    assert "Survey of 4 failed-job logs (from 15553 available)" in report
    assert "Missing log files (skipped): 1" in report
    assert "mocha" in report and "other" in report
    assert "Exceptions caught by parse_log: 0 (must be 0)" in report
    assert "with at least one failing name: 2 (100.0%, target 95%)" in report
    assert "the failures found agree with every summary (confidence 1.0): 1 (50.0%)" in report
    assert "Confidence" in report and "Worst misses" in report
    assert "job 2  a/a" in report
    # no log content reaches the report
    assert "npm ERR" not in report and "Error: x" not in report


def test_main_without_a_database_url_exits_with_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--n", "5"]) == EXIT_USAGE
    assert "DATABASE_URL is not set" in capsys.readouterr().err
