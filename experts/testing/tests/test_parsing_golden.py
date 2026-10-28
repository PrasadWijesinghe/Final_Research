"""Golden tests: every real fixture log is parsed and compared with its hand-checked expectations.

The `.expected.json` next to each fixture was written by reading the log (summary rows, failure
headers, first stack frame), not by dumping parser output. See `make_fixtures.py` for how the
fixtures are built and `tests/fixtures/logs/manifest.yaml` for why each job was chosen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from testing_expert.parsing import ParsedLog, parse_log
from testing_expert.parsing.make_fixtures import fixture_name, load_manifest

LOGS = Path(__file__).parent / "fixtures" / "logs"
ENTRIES = load_manifest(LOGS / "manifest.yaml").fixtures
SUMMARY_KEYS = ("passed", "failed", "skipped", "todo", "total", "files_failed")


def _paths(entry_framework: str, repo: str, job_id: int) -> tuple[Path, Path]:
    stem = fixture_name(repo, job_id)
    return LOGS / entry_framework / f"{stem}.log", LOGS / entry_framework / f"{stem}.expected.json"


CASES = [
    pytest.param(*_paths(e.framework, e.repo, e.job_id), id=f"{e.framework}/{e.repo}/{e.job_id}")
    for e in ENTRIES
]


def _first_project_path(result: ParsedLog) -> str | None:
    if not result.failures:
        return None
    return next((f.path for f in result.failures[0].frames if f.kind == "project"), None)


def _distinct_files(result: ParsedLog) -> list[str]:
    files: list[str] = []
    for failure in result.failures:
        if failure.file and failure.file not in files:
            files.append(failure.file)
    return files


@pytest.mark.parametrize(("log", "expected_path"), CASES)
def test_fixture_matches_its_expectations(log: Path, expected_path: Path) -> None:
    expected: dict[str, Any] = json.loads(expected_path.read_text(encoding="utf-8"))
    result = parse_log(log)

    assert result.framework == expected.get("parsed_framework", expected["framework"])
    assert result.parsed_ok is expected["parsed_ok"]
    assert result.confidence == expected["confidence"]
    assert result.granularity == expected["granularity"]
    assert result.summary_count == expected["summaries_found"]
    if expected["summary"] is None:
        assert result.summary is None
    else:
        assert result.summary is not None
        for key, value in expected["summary"].items():
            assert getattr(result.summary, key) == value, key
    assert [f.full_name for f in result.failures] == expected["failed_test_names"]
    assert _distinct_files(result) == expected["failed_files"]
    kinds = {k: sum(1 for f in result.failures if f.kind == k) for k in ("suite", "unhandled")}
    assert {k: v for k, v in kinds.items() if v} == expected.get("failure_kinds", {})
    first = result.failures[0] if result.failures else None
    assert (first.error_type if first else None) == expected["first_error_type"]
    assert _first_project_path(result) == expected["first_project_path"]


@pytest.mark.parametrize(("log", "expected_path"), CASES)
def test_failure_counts_agree_with_the_summary(log: Path, expected_path: Path) -> None:
    """When the parser trusts a result (confidence 1.0), the failures it found match the totals."""
    expected: dict[str, Any] = json.loads(expected_path.read_text(encoding="utf-8"))
    result = parse_log(log)
    if not (result.parsed_ok and result.confidence == 1.0 and result.summary):
        pytest.skip("not a fully trusted result")
    tests = [f for f in result.failures if f.kind == "test"]
    repeated = expected.get("repeated_failure_names", 0)
    reported = result.summary.failed or 0
    if result.summary_count == 1:
        assert len(tests) + repeated == reported
    else:  # retries print the same failure again, so unique names can be fewer than the total
        assert len(tests) <= reported
    if reported == 0 and (result.summary.files_failed or 0) > 0:
        assert any(f.kind == "suite" for f in result.failures)


@pytest.mark.parametrize(("log", "expected_path"), CASES)
def test_an_untrusted_result_says_why(log: Path, expected_path: Path) -> None:
    """A mismatch (confidence 0.7) always comes with a warning that names the counts."""
    result = parse_log(log)
    if result.confidence == 0.7:
        assert any("summar" in w for w in result.warnings)


def test_every_manifest_entry_has_a_fixture_and_an_expectation() -> None:
    for entry in ENTRIES:
        log, expected = _paths(entry.framework, entry.repo, entry.job_id)
        assert log.is_file(), log
        assert expected.is_file(), expected


def test_every_fixture_is_listed_in_the_manifest() -> None:
    listed = {_paths(e.framework, e.repo, e.job_id)[0] for e in ENTRIES}
    assert set(LOGS.glob("*/*.log")) == listed


def test_the_golden_set_covers_every_framework_and_the_odd_cases() -> None:
    """Guards the corpus itself: ten parsed logs per framework, hooks, suites, retries, nesting."""
    results = [parse_log(_paths(e.framework, e.repo, e.job_id)[0]) for e in ENTRIES]
    by_framework = {
        fw: sum(1 for r in results if r.framework == fw) for fw in ("jest", "mocha", "vitest")
    }
    assert all(count >= 10 for count in by_framework.values()), by_framework
    assert any(f.hook for r in results for f in r.failures)
    assert any(f.kind == "suite" for r in results for f in r.failures)
    assert any(f.kind == "unhandled" for r in results for f in r.failures)
    assert any(r.summary_count > 1 for r in results)
    assert {r.granularity for r in results} == {"test", "file", "none"}
