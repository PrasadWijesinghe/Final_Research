"""Parser for Mocha output (spec, dot, min and progress reporters).

What it reads, in the cleaned lines of one log:
- Epilogue: `N passing (1m)`, `N pending`, `N failing` in column 2, then one numbered block per
  failure (`  1) Suite` / `       test name:` / `     Error: message` / `      at ...`). A log can
  hold several epilogues (a monorepo runs mocha once per package); each is one summary.
- Per-test lines of the spec reporter (`✔ name (12ms)`, `- pending`, `1) failing`), with the
  `describe` names rebuilt from indentation. The dot, min and progress reporters print nothing per
  test, so for them only the epilogue is read and the granularity is "none".

Mocha prints no file names, so a failure's file is the first project frame of its stack that looks
like a test file (else the first project frame), with `file_source="stack"`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from testing_expert.parsing._common import (
    Budget,
    PartialParse,
    Run,
    build_failure,
    dedent,
    dedupe_failures,
    dedupe_results,
    last_duration_ms,
    make_run,
    prepare_lines,
    split_paren_suffix,
)
from testing_expert.parsing.models import CaseResult, FailedTest, Granularity, Outcome, Summary
from testing_expert.parsing.settings import ParsingConfig, load_config

_GROUP = re.compile(r"^  (?P<n>\d+) (?P<label>passing|pending|failing)\b(?P<rest>.*)$")
_HEADER = re.compile(r"^  (?P<n>\d+)\) ?(?P<text>.*)$")
# Lines reach these patterns right-stripped and capped (see `prepare_lines`), so none of them ends
# in `\s*$`: with a lazy `.*?` before it that is quadratic on a line of spaces.
_TICK = re.compile(r"^(?P<indent>\s+)[✔✓√]\s+(?P<text>\S.*)$")
_PENDING = re.compile(r"^(?P<indent>\s+)-\s+(?P<text>\S.*)$")
_FAIL_MARK = re.compile(r"^(?P<indent>\s+)(?P<n>\d+)\)\s+(?P<text>\S.*)$")
_DURATION = re.compile(r"(?P<n>\d+)ms")
_HOOK = re.compile(r'^"(?P<hook>before all|after all|before each|after each)" hook\b')
_CUT = re.compile(r"^\[(?:fixture trimmed|log truncated): \d+ lines omitted\]$")
_BODY_INDENT = 5  # error text of a numbered block starts five columns in
# DEFAULT: a failure title is read for at most this many lines (a describe nesting this deep is
# not seen in real logs; a block header with no closing colon stops sooner).
_MAX_TITLE_LINES = 12


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return not line.strip()


def _read_group(lines: Sequence[str], start: int) -> tuple[Summary, int]:
    """Read the `N passing / pending / failing` lines at `start`. Returns (summary, next index)."""
    counts: dict[str, int] = {}
    duration: float | None = None
    j = start
    while j < len(lines):
        m = _GROUP.match(lines[j])
        if not m:
            break
        counts.setdefault(m.group("label"), int(m.group("n")))
        if m.group("label") == "passing":
            duration = last_duration_ms(m.group("rest"))
        j += 1
    passed, failed, pending = (counts.get(k, 0) for k in ("passing", "failing", "pending"))
    # DEFAULT: mocha prints no total, so total = passing + failing + pending.
    return (
        Summary(
            passed=passed,
            failed=failed,
            skipped=pending,
            total=passed + failed + pending,
            duration_ms=duration,
        ),
        j,
    )


def _title(lines: Sequence[str], i: int, stop: int) -> tuple[list[str], int]:
    """Title parts of the failure whose header is at `i`, and the index of its last title line.

    Mocha prints each part of the title on its own line, two columns deeper than the one before
    (the first after `N) `, then at column 7, 9, 11, ...) and puts the colon after the last one.
    A describe name can itself end in a colon, so the colon alone does not end the title.
    """
    m = _HEADER.match(lines[i])
    assert m is not None  # callers checked
    parts = [m.group("text").strip()]
    k = i
    while k + 1 < stop and len(parts) < _MAX_TITLE_LINES:
        nxt = lines[k + 1]
        if _is_blank(nxt) or _indent(nxt) != _BODY_INDENT + 2 * len(parts):
            break
        k += 1
        parts.append(nxt.strip())
    if parts[-1].endswith(":"):
        parts[-1] = parts[-1][:-1].rstrip()
    return parts, k


def _read_failures(
    lines: Sequence[str], start: int, failing: int, config: ParsingConfig, budget: Budget
) -> tuple[list[FailedTest], int]:
    """Read the numbered failure blocks after an epilogue. Returns (failures, next index).

    Once the budget is spent the blocks are still walked (so the lines after the epilogue are read
    as ordinary output) but not built, and they are not counted.
    """
    n = len(lines)
    limit = config.parse.max_body_lines
    found: list[FailedTest] = []
    expected = 1
    j = start
    while j < n and _is_blank(lines[j]):
        j += 1
    while j < n:
        m = _HEADER.match(lines[j])
        if not m or int(m.group("n")) != expected:
            break
        parts, title_end = _title(lines, j, n)
        body_start = title_end + 1
        k = body_start
        seen_frame = False
        saw_blank = False
        is_last = expected >= failing
        while k < n:
            line = lines[k]
            nxt = _HEADER.match(line)
            if nxt and int(nxt.group("n")) == expected + 1:
                break
            if _GROUP.match(line) or _CUT.match(line):
                break
            if _is_blank(line):
                saw_blank = True
            else:
                if is_last and saw_blank and seen_frame and _indent(line) == 0:
                    break  # the last block is over: ordinary output follows
                if re.match(r"^\s+at\s", line):
                    seen_frame = True
                saw_blank = False
            k += 1
        # A block longer than `max_body_lines` is cut when it is parsed, but still read to its end
        # so the numbered blocks after it are found.
        body_end = min(k, body_start + limit)
        if budget.take():
            hook_match = next((h for p in parts if (h := _HOOK.match(p))), None)
            found.append(
                build_failure(
                    parts,
                    dedent(lines[body_start:body_end], _BODY_INDENT),
                    config,
                    hook=hook_match.group("hook") if hook_match else None,
                )
            )
        expected += 1
        j = k
        while j < n and _is_blank(lines[j]):
            j += 1
    return found, max(j, start)


def parse(lines: Sequence[str], config: ParsingConfig | None = None) -> PartialParse:
    """Parse Mocha output lines into failures, results and runs. Never raises on odd text."""
    cfg = config or load_config()
    lines = prepare_lines(lines, cfg.parse.max_line_chars)
    n = len(lines)
    budget = Budget(cfg.parse.max_failures)
    runs: list[Run] = []
    failures: list[FailedTest] = []
    results: list[CaseResult] = []
    stack: list[tuple[int, str]] = []
    i = 0
    while i < n:
        line = lines[i]
        if _GROUP.match(line):
            summary, i = _read_group(lines, i)
            block: list[FailedTest] = []
            if summary.failed:
                block, i = _read_failures(lines, i, summary.failed, cfg, budget)
                failures.extend(block)
            runs.append(make_run(summary, block))
            stack.clear()
            continue
        i += 1
        if _is_blank(line):
            continue
        outcome: Outcome | None = None
        indent = _indent(line)
        text = ""
        m = _TICK.match(line)
        if m:
            outcome, text = "passed", m.group("text")
        elif indent >= 4 and (m := _FAIL_MARK.match(line)):
            outcome, text = "failed", m.group("text")
        elif indent >= 4 and stack and indent > stack[0][0] and (m := _PENDING.match(line)):
            outcome, text = "skipped", m.group("text")
        if outcome is not None and m is not None:
            duration = None
            paren = split_paren_suffix(text)  # `name (12ms)`
            if paren is not None:
                dm = _DURATION.fullmatch(paren[1])
                if dm:
                    text, duration = paren[0], float(dm.group("n"))
            parents = [t for ind, t in stack if ind < indent]
            results.append(CaseResult(" > ".join([*parents, text]), None, outcome, duration))
        elif indent >= 2:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, line.strip()))
    kept, cut = dedupe_failures(failures, cfg.parse.max_failures)
    unique, results_cut = dedupe_results(results, cfg.parse.max_results)
    warnings: list[str] = []
    if budget.exhausted or cut:
        warnings.append(
            f"mocha: stopped after {cfg.parse.max_failures} failure blocks; the rest are not read"
        )
    if results_cut:
        warnings.append(f"mocha: more than {cfg.parse.max_results} results; the rest are dropped")
    granularity: Granularity = "test" if unique else "none"
    return PartialParse(
        results=tuple(unique),
        failures=tuple(kept),
        runs=tuple(runs),
        granularity=granularity,
        warnings=tuple(warnings),
        truncated=budget.exhausted or cut,
    )
