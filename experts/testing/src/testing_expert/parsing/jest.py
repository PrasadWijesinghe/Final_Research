"""Parser for Jest output (`jest`, also `jest-circus` and `jest-jasmine2` runs).

What it reads, in the cleaned lines of one log:
- Summaries: `Test Suites: ...` plus `Tests: ...` (and `Time:`). Both must start in column 0, so a
  nested jest run printed inside an error body (indented) is not mistaken for the real run.
  One log can hold several summaries (retries, several jest commands).
- Failures: `  ● Describe › test` blocks (exactly two spaces), plus babel's own reporter form
  (a bare `a > b > c` line under a `FAIL file` header). Each run's `Summary of all failing tests`
  section is used when there is one; otherwise the blocks printed while the tests ran.
- Results: `PASS`/`FAIL` file lines, and the `✓`/`✕`/`○`/`✎` test lines of the verbose reporter.

A `● Test suite failed to run` block is a suite failure (`kind="suite"`): the whole file could not
run, which does not count as a failed test in the `Tests:` line.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from testing_expert.parsing._common import (
    Budget,
    PartialParse,
    Run,
    build_failure,
    count_tokens,
    dedent,
    dedupe_failures,
    dedupe_results,
    duration_ms,
    make_run,
    prepare_lines,
    split_paren_suffix,
)
from testing_expert.parsing.models import (
    CaseResult,
    FailedTest,
    FailureKind,
    FileSource,
    Granularity,
    Outcome,
    Summary,
)
from testing_expert.parsing.settings import ParsingConfig, load_config
from testing_expert.parsing.stack import map_path

# DEFAULT: a failing suite (a file that could not run) has no test name; it is named as jest names
# it, and vitest suite failures use the same text, so the name is the same for both.
SUITE_FAILED_NAME = "Test suite failed to run"

_SUITES = re.compile(r"^Test Suites:\s+(?P<rest>\d.*)$")
_TESTS = re.compile(r"^Tests:\s+(?P<rest>\d.*)$")
_TIME = re.compile(r"^Time:\s+(?P<n>\d+(?:\.\d+)?)\s*(?P<u>ms|s|m)\b")
# DEFAULT: a summary whose `Ran all test suites` line ends in a closing quote is a jest run quoted
# inside a failure message, not a run of this log; `·` at the end of a block name marks the same
# nested text (jest prints a trailing space as `·` there). Both are heuristics from real logs.
_QUOTED_END_PREFIX = "Ran all test suites"
_SUMMARY_HEADER = "Summary of all failing tests"
# Lines reach these patterns right-stripped and at most `parse.max_line_chars` long, so none of
# them ends in `\s*$` (with a lazy `.*?` before it that would be quadratic on a line of spaces).
_FILE_HEADER = re.compile(r"^ ?(?P<kind>PASS|FAIL)\s+(?P<rest>\S.*)$")
_HEADER_DURATION = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*(?P<u>ms|s|m)")
_BLOCK_HEADER = re.compile(r"^  ● (?P<name>\S.*)$")
_PATH_LINE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\./|\.\./)?[\w@.+~-]+(?:[\\/][\w@.+~-]+)*\.\w{1,5}$")
_TICK = re.compile(
    r"^(?P<indent>\s+)(?P<mark>[✓✔√✕✗×○✎])\s+(?P<text>\S.*)$"
)  # a verbose-reporter test line
_TICK_DURATION = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*ms")
_SKIPPED_PREFIX = re.compile(r"^(?:skipped|todo)\s+")
_NOT_A_TEST_BLOCK = ("Console",)
_NOT_A_TEST_PREFIXES = ("Deprecation Warning", "Validation Warning", "Multiple configurations")
# Lines at the left margin that are runner output, never the name line of babel's reporter.
_RUNNER_LINE = re.compile(
    r"^(?:PASS|FAIL|Test Suites:|Tests:|Snapshots:|Time:|Ran all test suites|Summary of all)"
)
_BODY_INDENT = 4  # error text of a block is indented four spaces
_MARK_OUTCOME: dict[str, Outcome] = {
    "✓": "passed",
    "✔": "passed",
    "√": "passed",
    "✕": "failed",
    "✗": "failed",
    "×": "failed",
    "○": "skipped",
    "✎": "todo",
}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return not line.strip()


def _is_quoted_run(lines: Sequence[str], index: int) -> bool:
    """True when the summary at `index` ends in a closing quote: a nested run inside a message.

    The dot reporter prints a failed matcher's `Received string: "..."` without indenting it, so a
    jest run that an end-to-end test captured shows up at the left margin. Its last line is
    `Ran all test suites."`, with the quote; a real run's is not.
    """
    return any(
        line.startswith(_QUOTED_END_PREFIX) and line.endswith('"')
        for line in lines[index : index + 8]
    )


def _summaries(lines: Sequence[str]) -> list[tuple[int, Summary]]:
    """Every run's (line index of its last summary line, Summary), in log order."""
    suites: list[tuple[int, dict[str, int]]] = []
    tests: list[tuple[int, dict[str, int]]] = []
    for i, line in enumerate(lines):
        if not line.startswith("Test"):
            continue
        m = _SUITES.match(line)
        if m:
            if not _is_quoted_run(lines, i):
                suites.append((i, count_tokens(m.group("rest"))))
            continue
        m = _TESTS.match(line)
        if m and not _is_quoted_run(lines, i):
            tests.append((i, count_tokens(m.group("rest"))))
    used: set[int] = set()
    runs: list[tuple[int, Summary]] = []

    def duration_after(index: int) -> float | None:
        for line in lines[index + 1 : index + 4]:
            m = _TIME.match(line)
            if m:
                return duration_ms(m.group("n"), m.group("u"))
        return None

    # A `Tests:` line pairs with the first unused `Test Suites:` line up to 4 lines above it. Both
    # lists are in line order, so a pointer only moves forward: suites that are too far above
    # this `Tests:` line are too far above every later one (linear, not a scan per summary).
    pointer = 0
    for t_index, counts in tests:
        while pointer < len(suites) and suites[pointer][0] < t_index - 4:
            pointer += 1
        k = pointer
        pair: int | None = None
        while k < len(suites) and suites[k][0] < t_index:
            if k not in used:
                pair = k
                break
            k += 1
        files_failed = None
        if pair is not None:
            used.add(pair)
            files_failed = suites[pair][1].get("failed", 0)
        runs.append(
            (
                t_index,
                Summary(
                    passed=counts.get("passed", 0),
                    failed=counts.get("failed", 0),
                    skipped=counts.get("skipped", 0),
                    todo=counts.get("todo", 0),
                    total=counts.get("total", 0),
                    files_failed=files_failed,
                    duration_ms=duration_after(t_index),
                ),
            )
        )
    for k, (s_index, s_counts) in enumerate(suites):
        if k not in used:  # a `Test Suites:` line with no `Tests:` line: the run was cut short
            runs.append(
                (
                    s_index,
                    Summary(
                        files_failed=s_counts.get("failed", 0), duration_ms=duration_after(s_index)
                    ),
                )
            )
    runs.sort(key=lambda item: item[0])
    return runs


def _file_name(raw: str, config: ParsingConfig) -> str:
    """A path from a header or a path line, with the runner workspace prefix removed."""
    mapped, _kind = map_path(raw, config)
    return mapped or raw


class _Header(NamedTuple):
    """A `PASS`/`FAIL` file line: which, the file, and the time in brackets when printed."""

    kind: str
    path: str
    duration_ms: float | None


def _match_header(line: str) -> _Header | None:
    """A `PASS`/`FAIL` file line, or None (`PASS rate was 90%` has no path in it)."""
    m = _FILE_HEADER.match(line)
    if m is None:
        return None
    rest = m.group("rest")
    duration: float | None = None
    paren = split_paren_suffix(rest)  # `path (7.2 s)`
    if paren is not None:
        dm = _HEADER_DURATION.fullmatch(paren[1])
        if dm:
            rest, duration = paren[0], duration_ms(dm.group("n"), dm.group("u"))
    words = rest.split()
    if not words:
        return None
    path = words[-1]  # `PASS  <project>  <path>`: the project label comes first
    if "/" not in path and "\\" not in path and "." not in path:
        return None
    return _Header(m.group("kind"), path, duration)


def _block_end(lines: Sequence[str], start: int, stop: int, limit: int) -> int:
    """Index after an error block: it ends at the first non-blank line indented less than four."""
    j = start
    while j < stop and j - start < limit:
        line = lines[j]
        if not _is_blank(line) and _indent(line) < _BODY_INDENT:
            break
        j += 1
    return j


def _looks_like_bare_name(lines: Sequence[str], i: int, stop: int) -> bool:
    """Babel's reporter: a non-indented name line directly followed by indented error text."""
    line = lines[i]
    if _is_blank(line) or _indent(line) != 0 or _RUNNER_LINE.match(line) or line.startswith("●"):
        return False
    nxt = lines[i + 1] if i + 1 < stop else ""
    return not _is_blank(nxt) and _indent(nxt) >= 2


def _failures_in(
    lines: Sequence[str], start: int, stop: int, config: ParsingConfig, budget: Budget
) -> list[FailedTest]:
    """Failure blocks between `start` and `stop`; stops reading when the budget runs out."""
    found: list[FailedTest] = []
    limit = config.parse.max_body_lines
    cur_file: str | None = None
    cur_passed = False  # a `● name` block under a PASS line is a warning, not a failure
    expect_bare = False
    i = start
    while i < stop:
        line = lines[i]
        header = _match_header(line)
        if header:
            cur_file = _file_name(header.path, config)
            cur_passed = header.kind == "PASS"
            expect_bare = not cur_passed
            i += 1
            continue
        m = _BLOCK_HEADER.match(line)
        if m:
            expect_bare = False
            name = m.group("name")
            end = _block_end(lines, i + 1, stop, limit)
            if (
                cur_passed
                or name.endswith("·")  # a nested run quoted in a message (trailing space marker)
                or name in _NOT_A_TEST_BLOCK
                or name.startswith(_NOT_A_TEST_PREFIXES)
                or (name.endswith(":") and "›" not in name)
            ):
                i = max(end, i + 1)
                continue
            file: str | None = cur_file
            source: FileSource | None = "header" if cur_file else None
            for back in range(i - 1, max(i - 4, start - 1), -1):  # the dot reporter's path line
                prev = lines[back]
                if _is_blank(prev):
                    continue
                if _indent(prev) == 0 and _PATH_LINE.match(prev) and not _RUNNER_LINE.match(prev):
                    file, source = _file_name(prev, config), "header"
                break
            kind: FailureKind = "suite" if name == SUITE_FAILED_NAME else "test"
            parts = [p.strip() for p in name.split(" › ")]
            if not budget.take():
                break
            found.append(
                build_failure(
                    parts,
                    dedent(lines[i + 1 : end], _BODY_INDENT),
                    config,
                    file=file,
                    file_source=source,
                    kind=kind,
                )
            )
            i = max(end, i + 1)
            continue
        if expect_bare and not _is_blank(line):
            if _looks_like_bare_name(lines, i, stop):
                end = _block_end(lines, i + 1, stop, limit)
                parts = [p.strip() for p in line.strip().split(" > ")]
                if not budget.take():
                    break
                found.append(
                    build_failure(
                        parts,
                        dedent(lines[i + 1 : end], _BODY_INDENT),
                        config,
                        file=cur_file,
                        file_source="header" if cur_file else None,
                    )
                )
                i = max(end, i + 1)
                continue
            expect_bare = False
        i += 1
    return found


def _results(lines: Sequence[str], config: ParsingConfig) -> tuple[list[CaseResult], Granularity]:
    """File lines (`PASS`/`FAIL`) and, in verbose output, the test lines under each file line."""
    per_test: list[CaseResult] = []
    per_file: list[CaseResult] = []
    n = len(lines)
    i = 0
    while i < n:
        m = _match_header(lines[i])
        if not m:
            i += 1
            continue
        file = _file_name(m.path, config)
        outcome: Outcome = "passed" if m.kind == "PASS" else "failed"
        per_file.append(CaseResult(file, file, outcome, m.duration_ms))
        stack: list[tuple[int, str]] = []
        j = i + 1
        while j < n and not _is_blank(lines[j]):  # the verbose list ends at the first blank line
            line = lines[j]
            if _match_header(line):
                break
            tick = _TICK.match(line)
            if tick:
                indent = len(tick.group("indent"))
                text = tick.group("text")
                duration = None
                paren = split_paren_suffix(text)  # `name (5 ms)`
                if paren is not None:
                    dm = _TICK_DURATION.fullmatch(paren[1])
                    if dm:
                        text, duration = paren[0], float(dm.group("n"))
                text = _SKIPPED_PREFIX.sub("", text) if tick.group("mark") in "○✎" else text
                parents = [t for ind, t in stack if ind < indent]
                per_test.append(
                    CaseResult(
                        " > ".join([*parents, text]),
                        file,
                        _MARK_OUTCOME[tick.group("mark")],
                        duration,
                    )
                )
            elif _indent(line) >= 2:
                indent = _indent(line)
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, line.strip()))
            else:
                break
            j += 1
        i = max(j, i + 1)
    # DEFAULT: a file with test lines is represented by them; only files without test lines get a
    # file-level row (`name` and `file` are both the path).
    tested = {r.file for r in per_test}
    results = per_test + [r for r in per_file if r.file not in tested]
    granularity: Granularity = "test" if per_test else "file" if per_file else "none"
    return results, granularity


def parse(lines: Sequence[str], config: ParsingConfig | None = None) -> PartialParse:
    """Parse Jest output lines into failures, results and runs. Never raises on odd text."""
    cfg = config or load_config()
    lines = prepare_lines(lines, cfg.parse.max_line_chars)
    summaries = _summaries(lines)
    n = len(lines)
    bounds: list[tuple[int, int]] = []
    start = 0
    for end_index, _summary in summaries:
        bounds.append((start, end_index + 1))
        start = end_index + 1
    if start < n or not bounds:
        bounds.append((start, n))  # output after the last summary (or a log with no summary)
    failures: list[FailedTest] = []
    runs: list[Run] = []
    stray = 0
    budget = Budget(cfg.parse.max_failures)
    for k, (seg_start, seg_end) in enumerate(bounds):
        summary_at = next(
            (i for i in range(seg_start, seg_end) if lines[i] == _SUMMARY_HEADER), None
        )
        # DEFAULT: when a run printed `Summary of all failing tests`, only that section is read
        # (it repeats every failure once, with its file); else the blocks printed during the run.
        begin = summary_at + 1 if summary_at is not None else seg_start
        found = [] if budget.exhausted else _failures_in(lines, begin, seg_end, cfg, budget)
        failures.extend(found)
        if k < len(summaries):
            summary = summaries[k][1]
            # DEFAULT: blocks found in a run that reports no failures belong to another process
            # whose output is interleaved with this one (babel starts jest several times at once).
            # They are kept as failures but not counted against this run's summary.
            mine = found if (summary.failed or 0) + (summary.files_failed or 0) > 0 else []
            runs.append(make_run(summary, mine, unique=True))
        else:
            stray += sum(1 for f in found if f.kind == "test")
    kept, cut = dedupe_failures(failures, cfg.parse.max_failures)
    results, granularity = _results(lines, cfg)
    results, results_cut = dedupe_results(results, cfg.parse.max_results)
    warnings: list[str] = []
    if budget.exhausted or cut:
        warnings.append(
            f"jest: stopped after {cfg.parse.max_failures} failure blocks; the rest are not read"
        )
    if results_cut:
        warnings.append(f"jest: more than {cfg.parse.max_results} results; the rest are dropped")
    return PartialParse(
        results=tuple(results),
        failures=tuple(kept),
        runs=tuple(runs),
        stray=stray,
        granularity=granularity,
        warnings=tuple(warnings),
        truncated=budget.exhausted or cut,
    )
