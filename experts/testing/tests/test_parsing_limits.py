"""Time and size limits: hostile 32 KB lines, tens of thousands of summaries, a flood of failures.

Real logs have lines up to 32 KB (minified bundles) and can hold a million lines. Every pattern the
parsers use must run in linear time, and the parsers must stop building failures at the budget.
The adversarial lines come from a review that found cubic and quadratic cases (`⎯⎯⎯ ` and spaces,
`PASS a.js` and spaces then `(`, digit runs, long runs of spaces and tabs).
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Sequence

import pytest
from testing_expert.parsing import (
    ParsingConfig,
    clean_text,
    detect,
    jest,
    load_config,
    mocha,
    normalise,
    parse_frame,
    parse_log,
    signature,
    vitest,
)

TS = "2026-08-10T16:12:20.9525588Z"
LINE_CHARS = 32000  # the cleaner keeps lines up to 32 KB
BUDGET_SECONDS = 2.0  # DEFAULT: generous; a linear pass over these inputs takes milliseconds

# (prefix, filler): the shapes that were slow before the fix, and their neighbours.
SHAPES: list[tuple[str, str]] = [
    ("⎯⎯⎯ ", " "),
    ("⎯⎯⎯ ", "\t"),
    ("⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯", " "),
    (" ✓ a (1 tests) ", "1"),
    (" ✓ a (1 tests) ", " "),
    ("PASS a.js", " "),
    ("PASS a.js ", "\t"),
    ("FAIL ", " "),
    (" FAIL  ", " "),
    (" FAIL  a > ", " "),
    ("Test Suites: ", "1"),
    ("Tests: ", "1"),
    (" Test Files  ", "1"),
    ("      Tests  ", "1"),
    ("  ● a › ", " "),
    ("  ● ", "\t"),
    ("    at x (", " "),
    ("    at a", " "),
    (" ❯ x ", " "),
    ("  ✓ a", " "),
    ("  ✔ a", " "),
    ("   ✓ a", " "),
    ("  1) a", " "),
    ("     1) ", " "),
    ("  - ", " "),
    ("  1 passing", "1"),
    ("  1) ", "1"),
    ("    Error: ", "Error"),
    ("Ran all test suites", '"'),
    ("x ", "0123456789abcdef"),
    ("", "a/"),
    ("FAIL ", "a."),
    ("    at x (", "a:1:"),
]
ENDINGS = ["x", " (", "1)"]


def _hostile_lines() -> list[str]:
    lines = []
    for prefix, filler in SHAPES:
        for ending in ENDINGS:
            room = LINE_CHARS - len(prefix) - len(ending)
            lines.append(prefix + (filler * (room // len(filler) + 1))[:room] + ending)
    return lines


def _wide(config: ParsingConfig) -> ParsingConfig:
    """The same config with no effective line cap, so the patterns themselves are what is timed."""
    return dataclasses.replace(
        config,
        parse=dataclasses.replace(config.parse, max_line_chars=1_000_000),
        stack=dataclasses.replace(config.stack, max_line_chars=1_000_000),
    )


def _timed(fn: Callable[[], object]) -> float:
    started = time.perf_counter()
    fn()
    return time.perf_counter() - started


def _log(lines: Sequence[str]) -> str:
    wrapper = [
        f"{TS} ##[group]Run npx jest",
        f"{TS} ##[endgroup]",
        f"{TS} FAIL a.test.js",
        f"{TS}   ● a › b",
        f"{TS}     Error: x",
    ]
    tail = [
        f"{TS} Tests: 1 failed, 1 total",
        f"{TS}   1 passing (1s)",
        f"{TS}  Test Files  1 failed (1)",
    ]
    return "\n".join([*wrapper, *(f"{TS} {x}" for x in lines), *tail]) + "\n"


HOSTILE = _hostile_lines()


def test_the_hostile_lines_really_are_long() -> None:
    assert len(HOSTILE) == len(SHAPES) * len(ENDINGS)
    assert all(len(x) >= LINE_CHARS - 4 for x in HOSTILE)


def test_parse_log_is_fast_on_hostile_lines() -> None:
    text = _log(HOSTILE)
    assert _timed(lambda: parse_log(text)) < BUDGET_SECONDS * 4  # about 100 lines of 32 KB


@pytest.mark.parametrize("wide", [False, True], ids=["capped", "no-cap"])
@pytest.mark.parametrize(
    ("name", "parser"),
    [("jest", jest.parse), ("mocha", mocha.parse), ("vitest", vitest.parse)],
)
def test_each_parser_is_fast_on_hostile_lines(
    name: str, parser: Callable[..., object], wide: bool
) -> None:
    """With the cap and, to prove the patterns are linear themselves, without it."""
    config = _wide(load_config()) if wide else load_config()
    lines = clean_text(_log(HOSTILE), config).lines
    assert _timed(lambda: parser(lines, config)) < BUDGET_SECONDS * 4, name


@pytest.mark.parametrize("wide", [False, True], ids=["capped", "no-cap"])
def test_the_detector_is_fast_on_hostile_lines(wide: bool) -> None:
    config = _wide(load_config()) if wide else load_config()
    lines = clean_text(_log(HOSTILE), config).lines
    assert _timed(lambda: detect(lines, ["npx jest"], config=config)) < BUDGET_SECONDS * 4


def test_stack_frames_and_normalising_are_fast_on_hostile_lines() -> None:
    config = _wide(load_config())

    def run() -> None:
        for line in HOSTILE[:: max(len(HOSTILE) // 24, 1)]:
            parse_frame(line, config)
            normalise(line, config)
            signature("Error", line, [], config)

    assert _timed(run) < BUDGET_SECONDS * 2


def test_a_single_huge_run_of_spaces_after_a_vitest_rule_is_not_cubic() -> None:
    """The review's repro: `⎯⎯⎯ ` + N spaces + `x` took 7 s at N=2000 and 60 s at N=4000."""
    line = "⎯⎯⎯ " + " " * 20000 + "x"
    lines = [line] * 5
    assert _timed(lambda: vitest.parse(lines, _wide(load_config()))) < BUDGET_SECONDS


# ---------------------------------------------------------------- many summaries and failures


def test_fifty_thousand_jest_summaries_pair_in_linear_time() -> None:
    lines = ["Test Suites: 1 passed, 1 total", "Tests:       1 passed, 1 total"] * 50_000
    started = time.perf_counter()
    result = jest.parse(lines)
    assert time.perf_counter() - started < BUDGET_SECONDS * 2
    assert len(result.runs) == 50_000
    assert all(r.summary.files_failed == 0 for r in result.runs)  # each pair was paired


def test_fifty_thousand_vitest_summaries_pair_in_linear_time() -> None:
    lines = [" Test Files  1 passed (1)", "      Tests  1 passed (1)"] * 50_000
    started = time.perf_counter()
    result = vitest.parse(lines)
    assert time.perf_counter() - started < BUDGET_SECONDS * 2
    assert len(result.runs) == 50_000
    assert all(r.summary.files_failed == 0 for r in result.runs)


def test_a_tests_row_without_a_nearby_files_row_stays_unpaired() -> None:
    lines = [" Test Files  1 failed (1)", *["noise"] * 400, "      Tests  1 failed (1)"]
    result = vitest.parse(lines)
    assert {(r.summary.files_failed, r.summary.failed) for r in result.runs} == {
        (None, 1),
        (1, None),
    }


def _jest_blocks(n: int) -> list[str]:
    lines: list[str] = []
    for i in range(n):
        lines += [
            f"FAIL t{i}.test.js",
            f"  ● s › t{i}",
            "",
            "    Error: x",
            "      at f (src/a.js:1:1)",
            "",
        ]
    return lines + [f"Test Suites: {n} failed, {n} total", f"Tests:       {n} failed, {n} total"]


def _mocha_blocks(n: int) -> list[str]:
    lines = [f"  {n} failing", ""]
    for i in range(1, n + 1):
        lines += [
            f"  {i}) s",
            f"       t{i}:",
            "     Error: x",
            "      at f (test/a.test.js:1:1)",
            "",
        ]
    return lines


def _vitest_blocks(n: int) -> list[str]:
    lines: list[str] = []
    for i in range(n):
        lines += [
            f" FAIL  a.test.ts > s > t{i}",
            "Error: x",
            " ❯ a.test.ts:1:1",
            "",
            "⎯" * 10 + f"[{i}/{n}]⎯",
        ]
    return lines + [" Test Files  1 failed (1)", f"      Tests  {n} failed ({n})"]


@pytest.mark.parametrize(
    ("name", "parser", "make"),
    [
        ("jest", jest.parse, _jest_blocks),
        ("mocha", mocha.parse, _mocha_blocks),
        ("vitest", vitest.parse, _vitest_blocks),
    ],
)
def test_failures_stop_at_the_budget_and_the_result_says_so(
    name: str, parser: Callable[..., object], make: Callable[[int], list[str]]
) -> None:
    budget = load_config().parse.max_failures
    lines = make(budget * 20)
    started = time.perf_counter()
    result = parser(lines)
    elapsed = time.perf_counter() - started
    assert len(result.failures) == budget  # type: ignore[attr-defined]
    assert result.truncated is True  # type: ignore[attr-defined]
    assert any("stopped after" in w for w in result.warnings)  # type: ignore[attr-defined]
    assert elapsed < BUDGET_SECONDS * 2, name


def test_parse_log_marks_a_flood_of_failures_truncated_with_a_warning() -> None:
    budget = load_config().parse.max_failures
    text = "\n".join(f"{TS} {x}" for x in _jest_blocks(budget * 5)) + "\n"
    result = parse_log(text)
    assert result.truncated is True
    assert len(result.failures) == budget
    assert result.confidence == 0.7  # the summary says 5000, 1000 were built
    assert any("stopped after" in w for w in result.warnings)


def test_a_log_just_under_the_budget_is_not_marked_truncated() -> None:
    budget = load_config().parse.max_failures
    result = parse_log("\n".join(_jest_blocks(budget)) + "\n")
    assert result.truncated is False and len(result.failures) == budget
    assert result.confidence == 1.0
