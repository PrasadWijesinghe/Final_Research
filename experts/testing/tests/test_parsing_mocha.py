"""Mocha parser: small inline logs for the shapes real logs have (golden tests cover real ones)."""

from __future__ import annotations

import textwrap

from testing_expert.parsing import load_config, mocha, parse_log


def _lines(text: str) -> list[str]:
    """Dedent a block, then give it mocha's two-column left margin."""
    return [f"  {x}" if x else x for x in textwrap.dedent(text).strip("\n").split("\n")]


def _spec() -> list[str]:
    """A spec-reporter log with mocha's real margins: describe at 2, tests at 4, error at 5."""
    return [
        "  math",
        "    ✔ adds (3ms)",
        "    ✔ subtracts",
        "    - divides later",
        "    nested",
        "      1) breaks",
        "",
        "  2 passing (15ms)",
        "  1 pending",
        "  1 failing",
        "",
        "  1) math",
        "       nested",
        "         breaks:",
        "     AssertionError [ERR_ASSERTION]: expected 1 to equal 2",
        "      at Context.<anonymous> (test/math.test.js:10:7)",
        "      at process.processImmediate (node:internal/timers:476:21)",
    ]


def test_the_epilogue_gives_the_summary_title_error_type_and_file() -> None:
    result = mocha.parse(_spec())
    (run,) = result.runs
    s = run.summary
    assert (s.passed, s.failed, s.skipped, s.total) == (2, 1, 1, 4)
    (failure,) = result.failures
    assert failure.name_parts == ("math", "nested", "breaks")
    assert failure.full_name == "math > nested > breaks"
    assert failure.error_type == "AssertionError"
    assert failure.file == "test/math.test.js"  # the first test-looking project frame
    assert failure.file_source == "stack"
    assert failure.hook is None
    assert (run.tests, run.files) == (1, 1)


def test_spec_lines_become_results_with_describe_names_and_durations() -> None:
    result = mocha.parse(_spec())
    by_name = {r.full_name: r for r in result.results}
    assert by_name["math > adds"].outcome == "passed" and by_name["math > adds"].duration_ms == 3.0
    assert by_name["math > subtracts"].outcome == "passed"
    assert by_name["math > divides later"].outcome == "skipped"
    assert by_name["math > nested > breaks"].outcome == "failed"
    assert result.granularity == "test"


def test_dot_and_min_reporters_give_no_results_and_none_granularity() -> None:
    result = mocha.parse(
        _lines(
            """
              1 passing (1s)
              1 failing

              1) suite
                   case:
                 Error: boom
                  at Context.<anonymous> (test/a.test.js:3:3)
            """
        )
    )
    assert result.granularity == "none" and result.results == ()
    assert [f.full_name for f in result.failures] == ["suite > case"]


def test_a_describe_name_ending_in_a_colon_does_not_end_the_title() -> None:
    result = mocha.parse(
        _lines(
            """
              1 failing

              1) model: updateOne:
                   bulkWrite option
                     keeps the value:
                 AssertionError: nope
                  at Context.<anonymous> (test/m.test.js:1:1)
            """
        )
    )
    assert result.failures[0].name_parts == (
        "model: updateOne:",
        "bulkWrite option",
        "keeps the value",
    )


def test_a_hook_failure_keeps_the_hook_name() -> None:
    result = mocha.parse(
        _lines(
            """
              1 failing

              1) Suite
                   "before all" hook for "first test":
                 Error: no database
                  at Context.<anonymous> (test/s.test.js:5:5)

              2) "after each" hook: reset for "other test":
                 TimeoutError: slow
            """
        )
    )
    first, second = result.failures
    assert first.hook == "before all"
    assert first.full_name == 'Suite > "before all" hook for "first test"'
    assert second.hook == "after each"
    assert second.name_parts == ('"after each" hook: reset for "other test"',)
    assert second.error_type == "TimeoutError"


def test_several_epilogues_are_separate_runs_added_by_parse_log() -> None:
    text = (
        "  3 passing (1s)\n  1 pending\n\n"
        "  4 passing (2s)\n  1 failing\n\n  1) pkg\n       case:\n     Error: x\n"
        "      at a (test/x.test.js:1:1)\n"
    )
    result = parse_log(text)
    assert result.framework == "mocha" and result.summary_count == 2
    assert result.summary is not None
    assert (result.summary.passed, result.summary.failed, result.summary.skipped) == (7, 1, 1)
    assert (
        result.summary.total == 9
    )  # DEFAULT: mocha prints no total, so passing + failing + pending
    assert result.confidence == 1.0


def test_every_failed_attempt_counts_but_identical_names_are_listed_once() -> None:
    """With retries mocha lists the same test several times; the summary counts each block."""
    block = "  {n}) Suite\n       flaky:\n     Error: x\n      at f (test/a.test.js:1:1)\n\n"
    text = "  3 failing\n\n" + "".join(block.format(n=n) for n in (1, 2, 3))
    result = parse_log(text)
    assert [f.full_name for f in result.failures] == ["Suite > flaky"]
    assert mocha.parse(text.split("\n")).runs[0].tests == 3
    assert result.confidence == 1.0


def test_a_block_longer_than_the_limit_does_not_hide_the_blocks_after_it() -> None:
    limit = load_config().parse.max_body_lines
    long_body = "".join(f"       line {i}\n" for i in range(limit * 2))
    text = (
        "  2 failing\n\n  1) Suite\n       long one:\n     Error: huge\n"
        + long_body
        + "\n  2) Suite\n       after it:\n     Error: small\n"
    )
    result = mocha.parse(text.split("\n"))
    assert [f.full_name for f in result.failures] == ["Suite > long one", "Suite > after it"]
    assert len(result.failures[0].raw) <= load_config().parse.failure_raw_max_chars


def test_the_error_type_can_be_on_a_later_line_and_a_quoted_line_can_sit_at_column_zero() -> None:
    result = mocha.parse(
        [
            "  1 failing",
            "",
            "  1) Query",
            "       rejects:",
            "     AssertionError [ERR_ASSERTION]: The input did not match. Input:",
            "",
            "'MongooseError: Empty filter'",
            "",
            "      at Context.<anonymous> (file:///home/runner/work/r/r/test/q.test.js:9:5)",
            "",
            "",
            "",
            "Process completed with exit code 1.",
        ]
    )
    (failure,) = result.failures
    assert failure.error_type == "AssertionError"
    assert failure.file == "test/q.test.js"
    assert "Process completed" not in failure.raw
    assert "MongooseError" in failure.raw


def test_windows_and_file_url_frames_become_repo_relative_paths() -> None:
    result = mocha.parse(
        _lines(
            """
              1 failing

              1) Suite
                   case:
                 Error: boom
                  at rm (node:internal/fs/promises:806:10)
                  at Context.<anonymous> (file:///D:/a/r/r/test/src/s.test.ts:152:13)
                  at x (D:\\a\\r\\r\\src\\lib.ts:1:1)
            """
        )
    )
    (failure,) = result.failures
    assert [f.path for f in failure.frames if f.kind == "project"] == [
        "test/src/s.test.ts",
        "src/lib.ts",
    ]
    assert failure.file == "test/src/s.test.ts"


def test_a_test_file_frame_beats_an_earlier_source_frame() -> None:
    result = mocha.parse(
        _lines(
            """
              1 failing

              1) Suite
                   case:
                 Error: boom
                  at f (src/code.ts:1:1)
                  at g (test/utils/helper.ts:2:2)
                  at h (test/real.test.ts:3:3)
            """
        )
    )
    assert result.failures[0].file == "test/real.test.ts"


def test_a_stack_with_no_project_frame_leaves_the_file_unknown() -> None:
    result = mocha.parse(
        _lines(
            """
              1 failing

              1) Suite
                   case:
                 Error: Timeout of 2000ms exceeded.
                  at listOnTimeout (node:internal/timers:585:17)
            """
        )
    )
    assert result.failures[0].file is None and result.failures[0].file_source is None


def test_the_trimmed_marker_ends_a_block_and_is_an_ordinary_line() -> None:
    result = parse_log(
        "  2 failing\n\n  1) Suite\n       a:\n     Error: x\n      at f (test/a.test.js:1:1)\n"
        "[fixture trimmed: 40 lines omitted]\n"
    )
    assert [f.full_name for f in result.failures] == ["Suite > a"]
    assert result.confidence == 0.7  # the summary says 2, one block is left
    assert result.parsed_ok is True


def test_all_tests_failing_prints_only_a_failing_line() -> None:
    result = mocha.parse(_lines("  1 failing\n\n  1) s\n       t:\n     Error: x\n"))
    assert result.runs[0].summary.passed == 0 and result.runs[0].summary.failed == 1


def test_a_nested_epilogue_with_deeper_indentation_is_not_a_run() -> None:
    result = mocha.parse(_lines("      1 passing\n  2 passing (1s)\n"))
    assert [r.summary.passed for r in result.runs] == [2]


def test_windows_tick_and_pending_marks_are_understood() -> None:
    result = mocha.parse(_lines("  suite\n    √ works (4ms)\n    - waits\n\n  1 passing\n"))
    assert {r.full_name: r.outcome for r in result.results} == {
        "suite > works": "passed",
        "suite > waits": "skipped",
    }
