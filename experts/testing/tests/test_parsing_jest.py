"""Jest parser: small inline logs for the shapes real logs have (golden tests cover real ones)."""

from __future__ import annotations

import textwrap

from testing_expert.parsing import jest, parse_log


def _lines(text: str) -> list[str]:
    return textwrap.dedent(text).strip("\n").split("\n")


FAILING = """
    FAIL packages/a/test/a.test.js (1.2 s)
      ● Outer › inner › does the thing

        expect(received).toBe(expected) // Object.is equality

        Expected: 1
        Received: 2

          at Object.<anonymous> (packages/a/test/a.test.js:10:5)
          at run (/home/runner/work/repo/repo/packages/a/src/run.js:3:9)

    Test Suites: 1 failed, 2 passed, 3 total
    Tests:       1 failed, 4 passed, 5 total
    Time:        1.5 s
"""


def test_a_failing_block_gives_name_file_frames_and_diff_free_message() -> None:
    result = jest.parse(_lines(FAILING))
    (failure,) = result.failures
    assert failure.full_name == "Outer > inner > does the thing"
    assert failure.name_parts == ("Outer", "inner", "does the thing")
    assert failure.file == "packages/a/test/a.test.js"
    assert failure.file_source == "header"
    assert failure.kind == "test"
    assert failure.message.startswith("expect(received).toBe(expected)")
    assert [f.path for f in failure.frames] == [
        "packages/a/test/a.test.js",
        "packages/a/src/run.js",  # the runner workspace prefix is removed
    ]
    (run,) = result.runs
    assert (run.summary.failed, run.summary.passed, run.summary.total) == (1, 4, 5)
    assert run.summary.files_failed == 1
    assert run.summary.duration_ms == 1500.0
    assert (run.tests, run.suites, run.files) == (1, 0, 1)


def test_a_diff_is_split_from_the_message() -> None:
    result = jest.parse(
        _lines(
            """
            FAIL a.test.js
              ● t

                expect(received).toEqual(expected)

                - Expected  - 1
                + Received  + 1

                  Array [
                -   1,
                +   2,
                  ]

                  at Object.<anonymous> (a.test.js:3:3)
            """
        )
    )
    (failure,) = result.failures
    assert failure.message == "expect(received).toEqual(expected)"
    assert failure.diff is not None and failure.diff.startswith("- Expected")
    assert "+   2," in failure.diff


def test_nested_jest_output_inside_an_error_body_is_not_a_second_run() -> None:
    result = jest.parse(
        _lines(
            """
            FAIL e2e/outer.test.ts
              ● outer › spawns jest

                expect(received).toMatch(expected)

                Received string:    "FAIL inner.test.js
                  ● inner › fails
                    TypeError: x is not a function
                PASS other.test.js
                Tests:       1 failed, 1 passed, 2 total"

                  at Object.<anonymous> (e2e/outer.test.ts:9:9)

            Test Suites: 1 failed, 1 total
            Tests:       1 failed, 1 total
            """
        )
    )
    assert [f.full_name for f in result.failures] == ["outer > spawns jest"]
    assert len(result.runs) == 1 and result.runs[0].summary.total == 1


def test_a_quoted_nested_run_at_the_left_margin_is_ignored() -> None:
    """The dot reporter prints a matcher's `Received string` without indenting it."""
    result = jest.parse(
        _lines(
            """
            expect(received).toMatch(expected)

            Received string:    "FAIL inner.test.js
            Test Suites: 1 failed, 1 passed, 2 total
            Tests:       1 passed, 1 total
            Ran all test suites."
            Test Suites: 1 failed, 4 passed, 5 total
            Tests:       1 failed, 9 passed, 10 total
            Ran all test suites.
            """
        )
    )
    assert [r.summary.total for r in result.runs] == [10]


def test_several_summaries_are_kept_and_added_by_parse_log() -> None:
    text = (
        "Test Suites: 1 passed, 1 total\nTests:       3 passed, 3 total\n"
        "FAIL a.test.js\n  ● a › one\n\n    boom\n\n  ● a › two\n\n    bang\n\n"
        "Test Suites: 1 failed, 1 total\nTests:       2 failed, 1 passed, 3 total\n"
    )
    result = parse_log(text)
    assert result.summary_count == 2
    assert result.summary is not None
    assert (result.summary.passed, result.summary.failed, result.summary.total) == (4, 2, 6)
    assert [f.full_name for f in result.failures] == ["a > one", "a > two"]
    assert result.confidence == 1.0


def test_retried_runs_list_the_failure_once_and_still_agree() -> None:
    attempt = (
        "FAIL a.test.js\n  ● flaky\n\n    boom\n\n"
        "Test Suites: 1 failed, 1 total\nTests:       1 failed, 1 total\n"
    )
    result = parse_log(attempt * 3)
    assert [f.full_name for f in result.failures] == ["flaky"]
    assert result.summary is not None and result.summary.failed == 3
    assert result.summary_count == 3
    assert result.confidence == 1.0  # each attempt matches its own summary


def test_summary_of_all_failing_tests_is_read_instead_of_the_blocks_printed_earlier() -> None:
    result = jest.parse(
        _lines(
            """
            FAIL a.test.js
              ● early

                first print

            Summary of all failing tests
             FAIL  a.test.js (2 s)
              ● summary name

                second print

            Test Suites: 1 failed, 1 total
            Tests:       1 failed, 1 total
            """
        )
    )
    assert [f.full_name for f in result.failures] == ["summary name"]


def test_suite_failed_to_run_is_a_suite_failure_not_a_failed_test() -> None:
    result = parse_log(
        "FAIL src/a.test.ts\n  ● Test suite failed to run\n\n    SyntaxError: nope\n\n"
        "      at compile (src/a.ts:1:1)\n\n"
        "Test Suites: 1 failed, 1 total\nTests:       0 total\n"
    )
    (failure,) = result.failures
    assert (failure.kind, failure.full_name, failure.file) == (
        "suite",
        "Test suite failed to run",
        "src/a.test.ts",
    )
    assert failure.error_type == "SyntaxError"
    assert result.summary is not None
    assert (result.summary.failed, result.summary.files_failed) == (0, 1)
    assert result.parsed_ok and result.confidence == 1.0


def test_failing_suites_are_not_counted_as_failing_tests() -> None:
    """`Test Suites: 24 failed` with `Tests: 100 passed`: no failed test, 24 failed files."""
    blocks = "".join(
        f"FAIL t/{i}.test.js\n  ● Test suite failed to run\n\n    boom\n\n" for i in range(3)
    )
    result = parse_log(
        blocks + "Test Suites: 3 failed, 1 passed, 4 total\nTests:       100 passed, 100 total\n"
    )
    assert sum(1 for f in result.failures if f.kind == "suite") == 3
    assert sum(1 for f in result.failures if f.kind == "test") == 0
    assert result.confidence == 1.0


def test_blocks_under_a_pass_line_are_warnings_not_failures() -> None:
    result = jest.parse(
        _lines(
            """
            PASS src/a.test.js
              ● a › skipped hook

                Cannot find module 'x'

            Test Suites: 1 passed, 1 total
            Tests:       1 passed, 1 total
            """
        )
    )
    assert result.failures == ()


def test_console_and_warning_blocks_are_not_failures() -> None:
    result = jest.parse(
        _lines(
            """
            PASS src/a.test.js
              ● Console

                console.log
                  hello

            FAIL src/b.test.js
              ● Validation Warning:

                Unknown option

              ● real › failure

                boom

            Tests:       1 failed, 1 total
            """
        )
    )
    assert [f.full_name for f in result.failures] == ["real > failure"]


def test_babel_reporter_names_a_failure_with_a_bare_line_under_the_fail_header() -> None:
    result = jest.parse(
        _lines(
            """
            FAIL packages/p/test/index.js
            suite name > some case > the leaf
                Error: Expected output to match.

                  at validate (packages/h/src/index.js:5:1)

            Test Suites: 1 failed, 1 total
            Tests:       1 failed, 1 total
            """
        )
    )
    (failure,) = result.failures
    assert failure.name_parts == ("suite name", "some case", "the leaf")
    assert failure.file == "packages/p/test/index.js"
    assert failure.error_type == "Error"


def test_the_dot_reporter_prints_a_path_line_before_the_block() -> None:
    result = jest.parse(
        _lines(
            """
            ..F..
            /home/runner/work/repo/repo/e2e/__tests__/a.test.ts

              ● a › does it

                nope

                  at Object.<anonymous> (e2e/__tests__/a.test.ts:4:5)

            ...
            Tests:       1 failed, 3 passed, 4 total
            """
        )
    )
    (failure,) = result.failures
    assert failure.file == "e2e/__tests__/a.test.ts"
    assert failure.file_source == "header"


def test_windows_paths_in_headers_and_frames_become_repo_relative() -> None:
    result = jest.parse(
        _lines(
            """
            FAIL packages/w/src/__tests__/w.test.ts
              ● w › fails

                TypeError: bad

                  at Object.<anonymous> (D:\\a\\j\\j\\packages\\w\\src\\__tests__\\w.test.ts:5:7)
                  at file:///D:/a/jest/jest/packages/w/src/w.js:2:3

            Tests:       1 failed, 1 total
            """
        )
    )
    (failure,) = result.failures
    assert [f.path for f in failure.frames] == [
        "packages/w/src/__tests__/w.test.ts",
        "packages/w/src/w.js",
    ]


def test_verbose_reporter_gives_test_results_with_describe_names_and_durations() -> None:
    result = jest.parse(
        _lines(
            """
            PASS src/a.test.js
              math
                ✓ adds (3 ms)
                nested
                  ✓ subtracts
                  ✕ divides (12 ms)
              ○ skipped later
              ✎ todo write more

            FAIL src/b.test.js
            """
        )
    )
    by_name = {r.full_name: r for r in result.results}
    assert by_name["math > adds"].outcome == "passed" and by_name["math > adds"].duration_ms == 3.0
    assert by_name["math > nested > subtracts"].outcome == "passed"
    assert by_name["math > nested > divides"].outcome == "failed"
    # jest prints `○ skipped <name>` and `✎ todo <name>`: the word is the status, not the name
    assert by_name["later"].outcome == "skipped"
    assert by_name["write more"].outcome == "todo"
    assert result.granularity == "test"
    # the file with test lines has no file-level row; the file without them does
    assert by_name["src/b.test.js"].outcome == "failed"
    assert "src/a.test.js" not in by_name


def test_file_lines_only_give_file_granularity() -> None:
    result = jest.parse(_lines("PASS src/a.test.js (5 s)\nFAIL src/b.test.js\n"))
    assert result.granularity == "file"
    assert [(r.full_name, r.outcome, r.duration_ms) for r in result.results] == [
        ("src/a.test.js", "passed", 5000.0),
        ("src/b.test.js", "failed", None),
    ]


def test_a_pass_word_that_is_not_a_file_is_ignored() -> None:
    assert jest.parse(_lines("PASS rate was 90%\n")).results == ()


def test_a_trimmed_marker_line_is_an_ordinary_line() -> None:
    result = parse_log(
        "FAIL a.test.js\n  ● t\n\n    boom\n\n[fixture trimmed: 30 lines omitted]\n"
        "Test Suites: 1 failed, 1 total\nTests:       1 failed, 1 total\n"
    )
    assert [f.full_name for f in result.failures] == ["t"]
    assert result.confidence == 1.0


def test_failures_without_a_summary_get_the_low_confidence() -> None:
    result = parse_log("FAIL a.test.js\n  ● killed › midway\n\n    boom\n\n    at f (a.js:1:1)\n")
    assert result.summary is None
    assert [f.full_name for f in result.failures] == ["killed > midway"]
    assert result.confidence == 0.4


def test_a_summary_that_disagrees_with_the_blocks_gets_the_mismatch_confidence() -> None:
    result = parse_log("Test Suites: 1 failed, 1 total\nTests:       2 failed, 1 total\n")
    assert result.parsed_ok is False  # failures reported, none found
    assert result.confidence == 0.7
    assert any("no failing test was found" in w for w in result.warnings)
