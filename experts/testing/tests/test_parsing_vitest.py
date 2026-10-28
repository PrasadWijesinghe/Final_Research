"""Vitest parser: small inline logs for the shapes real logs have (golden tests cover real ones)."""

from __future__ import annotations

from testing_expert.parsing import parse_log, vitest

RULE = "⎯" * 23


def _failed_section(
    *fails: str, body: str = "AssertionError: expected 1 to be 2", sep: str = "1/1"
) -> list[str]:
    lines = [f"{'⎯' * 7} Failed Tests {len(fails)} {'⎯' * 7}", ""]
    lines += [f" FAIL  {x}" for x in fails]
    lines += [
        body,
        " ❯ src/a.test.ts:10:5",
        "     10|   expect(1).toBe(2)",
        "",
        f"{'⎯' * 22}[{sep}]⎯",
        "",
    ]
    return lines


SUMMARY = [
    " Test Files  1 failed | 3 passed (4)",
    "      Tests  1 failed | 9 passed | 2 skipped (12)",
    "   Start at  14:56:41",
    "   Duration  15.48s (transform 5.56s)",
]


def test_a_failed_test_gives_name_file_error_type_frames_and_summary() -> None:
    result = vitest.parse(_failed_section("src/a.test.ts > suite > does it") + SUMMARY)
    (failure,) = result.failures
    assert failure.name_parts == ("suite", "does it")
    assert failure.file == "src/a.test.ts" and failure.file_source == "header"
    assert failure.error_type == "AssertionError"
    assert [f.path for f in failure.frames] == ["src/a.test.ts"]
    assert failure.frames[0].line == 10
    (run,) = result.runs
    s = run.summary
    assert (s.passed, s.failed, s.skipped, s.total, s.files_failed) == (9, 1, 2, 12, 1)
    assert s.duration_ms == 15480.0
    assert (run.tests, run.files) == (1, 1)


def test_a_project_badge_a_line_suffix_and_a_retry_suffix_are_removed() -> None:
    result = vitest.parse(
        _failed_section(
            "legacy  tests/a.test.ts > block > one",
            "|chromium| tests/b.test.ts:163 > two (retry x2)",
            "@scope/pkg  src/c.test.tsx > three",
        )
    )
    assert [(f.file, f.full_name, f.retries) for f in result.failures] == [
        ("tests/a.test.ts", "block > one", 0),
        ("tests/b.test.ts", "two", 2),
        ("src/c.test.tsx", "three", 0),
    ]


def test_several_fail_lines_share_one_error_body() -> None:
    result = vitest.parse(_failed_section("a.test.ts > one", "a.test.ts > two"))
    assert [f.full_name for f in result.failures] == ["one", "two"]
    assert result.failures[0].message == result.failures[1].message
    assert result.failures[0].error_type == "AssertionError"


def test_the_same_fail_line_twice_in_a_group_is_one_failure() -> None:
    result = vitest.parse(
        _failed_section("a.test.ts > one", "a.test.ts > one")
        + [" Test Files  1 failed (1)", "      Tests  1 failed (1)"]
    )
    assert len(result.failures) == 1
    assert (result.runs[0].tests, result.runs[0].files) == (1, 1)


def test_two_failing_attempts_of_one_test_count_once_for_the_run() -> None:
    text = (
        _failed_section("a.test.ts > flaky", sep="1/2")
        + _failed_section("a.test.ts > flaky", sep="2/2")
        + [" Test Files  1 failed (1)", "      Tests  1 failed (1)"]
    )
    result = parse_log("\n".join(text))
    assert len(result.failures) == 1
    assert result.confidence == 1.0


def test_failed_suites_are_suite_failures_with_a_file_and_no_test_name() -> None:
    lines = [
        f"{'⎯' * 6} Failed Suites 2 {'⎯' * 6}",
        "",
        " FAIL   pkg  tests/x.test.ts [ tests/x.test.ts ]",
        " FAIL   pkg  tests/y.test.ts [ tests/y.test.ts ]",
        "TypeError: Class extends value undefined",
        " ❯ Object.<anonymous> ../lib/src/a.ts:6:43",
        "",
        f"{'⎯' * 22}[1/2]⎯",
        " Test Files  2 failed (2)",
        "      Tests  no tests",
    ]
    result = vitest.parse(lines)
    assert [(f.kind, f.full_name, f.file) for f in result.failures] == [
        ("suite", "Test suite failed to run", "tests/x.test.ts"),
        ("suite", "Test suite failed to run", "tests/y.test.ts"),
    ]
    assert result.failures[0].error_type == "TypeError"
    assert result.runs[0].suites == 2 and result.runs[0].summary.files_failed == 2


def test_unhandled_errors_become_failures_of_kind_unhandled() -> None:
    lines = [
        f"{'⎯' * 6} Unhandled Errors {'⎯' * 6}",
        "",
        "Vitest caught 1 unhandled error during the test run.",
        "",
        f"{'⎯' * 3} Unhandled Rejection {'⎯' * 3}",
        "Error: connection reset",
        " ❯ src/db.ts:12:3",
        "",
        "⎯" * 25,
        " Test Files  1 passed (1)",
        "      Tests  3 passed (3)",
        "     Errors  1 error",
    ]
    result = vitest.parse(lines)
    (failure,) = result.failures
    assert failure.kind == "unhandled"
    assert failure.full_name == "Unhandled Rejection > Error: connection reset"
    assert failure.file == "src/db.ts"
    assert result.runs[0].tests == 0  # an unhandled error is not a failed test


def test_a_nested_vitest_run_inside_an_error_is_not_a_second_run() -> None:
    body = "\n".join(
        [
            "AssertionError: expected output",
            "",
            "- Expected",
            "+ Received",
            "",
            "      Test Files  1 failed (1)",
            "           Tests  1 failed (1)",
        ]
    )
    result = vitest.parse(_failed_section("a.test.ts > spawns vitest", body=body) + SUMMARY)
    assert len(result.runs) == 1 and result.runs[0].summary.total == 12


def test_windows_summary_rows_interleaved_with_the_error_text_are_filtered_out() -> None:
    lines = [
        f"{'⎯' * 7} Failed Tests 1 {'⎯' * 7}",
        " Test Files  1 failed | 68 passed (69)",
        "",
        "      Tests  1 failed | 920 passed (921)",
        " FAIL  a.spec.ts > block > case",
        "   Start at  05:18:31",
        "AssertionError: expected 'a' to be 'b' // Object.is equality",
        "   Duration  8.68s (transform 3.18s)",
        "",
        " ❯ a.spec.ts:57:34",
        "",
        f"{'⎯' * 22}[1/1]⎯",
    ]
    result = vitest.parse(lines)
    (failure,) = result.failures
    assert failure.error_type == "AssertionError"
    assert "Start at" not in failure.raw and "Duration" not in failure.raw
    assert [f.path for f in failure.frames] == ["a.spec.ts"]
    assert result.runs[0].summary.failed == 1  # the two summary rows still pair up


def test_watch_mode_prints_the_summary_before_its_failures() -> None:
    lines = (
        [
            " Test Files  1 failed (1)",
            "      Tests  1 failed (1)",
            " FAIL  Tests failed. Watching for file changes...",
        ]
        + _failed_section("a.test.ts > one")
        + [
            " Test Files  1 failed (1)",
            "      Tests  1 failed (1)",
            " FAIL  Tests failed. Watching for file changes...",
        ]
        + _failed_section("a.test.ts > one")
    )
    result = parse_log("\n".join(lines))
    assert [f.full_name for f in result.failures] == ["one"]
    assert result.summary_count == 2 and result.confidence == 1.0


def test_default_reporter_lines_give_file_results_and_keep_the_listed_test() -> None:
    lines = [
        " ✓  legacy  tests/a.test.ts (26 tests) 234ms",
        " ❯  legacy  tests/b.test.ts (12 tests | 1 failed) 368ms",
        "     ✓ works 17ms",
        "     × breaks 43ms (retry x1)",
        " ↓ tests/c.test.ts (3 tests | 3 skipped)",
    ]
    result = vitest.parse(lines)
    by_name = {r.full_name: r for r in result.results}
    assert (
        by_name["tests/a.test.ts"].outcome == "passed"
        and by_name["tests/a.test.ts"].duration_ms == 234.0
    )
    assert "tests/b.test.ts" not in by_name  # its own test lines stand in for the file line
    assert by_name["tests/c.test.ts"].outcome == "skipped"
    assert by_name["breaks"].outcome == "failed" and by_name["breaks"].file == "tests/b.test.ts"
    assert result.granularity == "file"  # the default reporter lists only some tests


def test_a_failed_result_takes_the_full_name_from_its_fail_block_and_the_retry_count() -> None:
    lines = [
        " ❯ tests/b.test.ts (2 tests | 1 failed) 5ms",
        "   × breaks 43ms (retry x1)",
        *_failed_section("tests/b.test.ts > outer > breaks"),
    ]
    result = vitest.parse(lines)
    assert [r.full_name for r in result.results if r.outcome == "failed"] == ["outer > breaks"]
    assert result.failures[0].retries == 1


def test_the_verbose_reporter_gives_test_granularity_with_describe_names() -> None:
    lines = [
        " ✓  proj  tests/lib/parse.test.ts (7 tests) 1958ms",
        "   ✓ semanticInfo (7)",
        "     ✓ works for arrays 1896ms",
        "     ✓ nested (2)",
        "       ✓ deep case 5ms",
    ]
    result = vitest.parse(lines)
    assert result.granularity == "test"
    assert [(r.full_name, r.duration_ms) for r in result.results] == [
        ("semanticInfo > works for arrays", 1896.0),
        ("semanticInfo > nested > deep case", 5.0),
    ]
    assert all(r.file == "tests/lib/parse.test.ts" for r in result.results)


def test_verbose_reporter_lines_with_the_whole_path_are_test_results() -> None:
    result = vitest.parse(
        ["  ✓ src/a.test.ts > math > adds 3ms", "  × src/a.test.ts > math > divides 1ms"]
    )
    assert [(r.file, r.full_name, r.outcome) for r in result.results] == [
        ("src/a.test.ts", "math > adds", "passed"),
        ("src/a.test.ts", "math > divides", "failed"),
    ]
    assert result.granularity == "test"


def test_a_tests_row_far_below_its_files_row_still_pairs_with_it() -> None:
    lines = [
        " Test Files  1 failed | 3 passed (4)",
        *["noise"] * 120,
        "      Tests  2 failed | 1 passed (3)",
    ]
    result = vitest.parse(lines)
    (run,) = result.runs
    assert (run.summary.failed, run.summary.files_failed) == (2, 1)


def test_vite_build_ticks_are_not_tests() -> None:
    result = vitest.parse(["✓ 1458 modules transformed.", " ✓ 12 modules transformed."])
    assert result.results == ()


def test_the_trimmed_marker_ends_a_failure_block() -> None:
    lines = [
        f"{'⎯' * 7} Failed Tests 1 {'⎯' * 7}",
        " FAIL  a.test.ts > one",
        "Error: boom",
        "[fixture trimmed: 99 lines omitted]",
        "more text that is not the error",
    ]
    (failure,) = vitest.parse(lines).failures
    assert "more text" not in failure.raw


def test_the_dot_reporter_prints_no_results_only_the_failure_section_and_the_summary() -> None:
    """A synthetic dot-reporter log: no real fixture exists for it (the repos that use it, vite and
    svelte, print file lines too), so this is the only coverage of the claim in the README."""
    text = "\n".join(
        [
            " RUN  v3.2.4 /home/runner/work/r/r",
            "",
            "·······×····↓··",
            "·········",
            "",
            f"{'⎯' * 7} Failed Tests 1 {'⎯' * 7}",
            "",
            " FAIL  src/a.test.ts > suite > case",
            "AssertionError: expected 1 to be 2",
            " ❯ src/a.test.ts:10:5",
            "",
            f"{'⎯' * 22}[1/1]⎯",
            "",
            " Test Files  1 failed | 2 passed (3)",
            "      Tests  1 failed | 21 passed | 1 skipped (23)",
            "   Start at  10:00:00",
            "   Duration  1.20s",
        ]
    )
    result = parse_log(text)
    assert result.framework == "vitest"
    assert result.granularity == "none" and result.results == ()
    assert [f.full_name for f in result.failures] == ["suite > case"]
    assert result.summary is not None and result.summary.total == 23
    assert result.confidence == 1.0
