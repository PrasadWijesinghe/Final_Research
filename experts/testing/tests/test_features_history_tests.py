"""Per-test / per-file history (`ht_*`): pure index, no database."""

from __future__ import annotations

import pytest
from testing_expert.features.history_tests import (
    EMPTY_TEST_HISTORY,
    FEATURE_NAMES,
    ResultRow,
    TestHistory,
)

DAY = 86_400.0
NOW = 1_800_000_000.0


def row(
    job: int, day: float, tid: str, failed: bool, file: str | None = None, gran: str = "test"
) -> ResultRow:
    return ResultRow(job, NOW + day * DAY, tid, file, failed, gran, int(gran == "test"))


def test_empty_history_is_unknown_not_zero_for_rates() -> None:
    f = EMPTY_TEST_HISTORY.features(NOW, ["a.test.js"])
    assert set(f) == set(FEATURE_NAMES)
    assert f["ht_parsed_runs_90d"] == 0 and f["ht_failed_tests_90d"] == 0
    assert f["ht_test_level"] is None and f["ht_last_run_failed_tests"] is None
    assert f["ht_touched_test_files"] == 1 and f["ht_touched_fail_rate"] is None


def test_job_level_counts_and_flaky_detection() -> None:
    rows = [
        row(1, -10, "t1", False), row(1, -10, "t2", False),
        row(2, -9, "t1", True), row(2, -9, "t2", False),   # t1 fails
        row(3, -8, "t1", False), row(3, -8, "t2", True),   # t1 passes again (flip), t2 fails
        row(4, -1, "t2", True), row(4, -1, "t1", False),   # t2 fails again
    ]  # fmt: skip
    h = TestHistory(rows)
    f = h.features(NOW, [])
    assert f["ht_parsed_runs_30d"] == 4
    assert f["ht_failed_tests_30d"] == 2
    assert f["ht_flaky_tests_30d"] == 2  # t1 and t2 both failed and passed
    assert f["ht_last_run_failed_tests"] == 1  # run 4: t2
    assert f["ht_persistent_failing_tests"] == 1  # t2 failed in runs 3 and 4
    assert f["ht_test_level"] == 1


def test_windows_exclude_old_failures() -> None:
    h = TestHistory([row(1, -80, "t1", True), row(2, -2, "t2", False)])
    f = h.features(NOW, [])
    assert f["ht_failed_tests_30d"] == 0 and f["ht_failed_tests_90d"] == 1
    assert f["ht_parsed_runs_30d"] == 1 and f["ht_parsed_runs_90d"] == 2


def test_strict_cutoff_ignores_equal_and_later_results() -> None:
    rows = [row(1, -5, "t1", True), row(2, 0, "t1", True), row(3, 4, "t1", True)]
    f = TestHistory(rows).features(NOW, [])
    assert f["ht_parsed_runs_90d"] == 1 and f["ht_failed_tests_90d"] == 1
    # adding later rows never changes the value at an earlier cutoff
    g = TestHistory(rows[:1]).features(NOW, [])
    assert f == g


def test_touched_files_rates_and_last_outcome() -> None:
    rows = [
        row(1, -9, "f1", False, "a.test.js", "file"), row(1, -9, "f2", False, "b.test.js", "file"),
        row(2, -5, "f1", True, "a.test.js", "file"), row(2, -5, "f2", False, "b.test.js", "file"),
        row(3, -1, "f1", False, "a.test.js", "file"), row(3, -1, "f2", False, "b.test.js", "file"),
    ]  # fmt: skip
    h = TestHistory(rows)
    f = h.features(NOW, ["a.test.js", "c.test.js"])
    assert f["ht_test_level"] == 0  # file level
    assert f["ht_touched_test_files"] == 2 and f["ht_touched_files_with_history"] == 1
    assert f["ht_touched_fail_rate"] == pytest.approx(1 / 3)
    assert f["ht_touched_last_failed"] == 0 and f["ht_touched_failures_90d"] == 1
    assert f["ht_touched_max_flip_rate"] == pytest.approx(1.0)
    none = h.features(NOW, ["zzz.test.js"])
    assert none["ht_touched_files_with_history"] == 0 and none["ht_touched_fail_rate"] is None


def test_test_level_failure_preferred_over_file_level_in_counts() -> None:
    rows = [  # jest style: the failing file and the failing test are both named
        row(1, -2, "file1", True, "a.test.js", "file"),
        row(1, -2, "test1", True, "a.test.js", "test"),
    ]
    f = TestHistory(rows).features(NOW, [])
    assert f["ht_last_run_failed_tests"] == 1 and f["ht_failed_tests_30d"] == 1


def test_parsed_run_without_rows_counts() -> None:
    h = TestHistory([], [(NOW - DAY, 9, 0)])
    f = h.features(NOW, [])
    assert f["ht_parsed_runs_30d"] == 1 and f["ht_last_run_failed_tests"] == 0
