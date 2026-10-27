"""Pieces the three framework parsers share: what a parser returns, and helpers for error blocks.

An error block is the text a runner prints for one failure: the message, an optional assertion
diff, a code frame and the stack. `build_failure` turns the lines of one block into a
`FailedTest`: it finds the error type, splits the message from the diff, parses the stack frames
and, when the runner printed no file name, takes the file from the first project stack frame.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from testing_expert.parsing.models import (
    CaseResult,
    FailedTest,
    FailureKind,
    FileSource,
    Granularity,
    StackFrame,
    Summary,
)
from testing_expert.parsing.settings import ParseSettings, ParsingConfig
from testing_expert.parsing.stack import parse_frame, parse_frames

# What the separate names of a test are joined with in `full_name`.
NAME_SEP = " > "

_ERROR_TYPE = re.compile(
    r"^\s*(?P<type>(?:[A-Z][\w$]*)?(?:Error|Exception))(?:\s*\[[^\]\n]*\])?\s*(?::|$)"
)
# A line of a code frame as jest (`> 106 |   expect(x)`) and vitest (`  114|   expect(x)`) print it.
_CODE_FRAME = re.compile(r"^\s*(?:>\s*)?\d+\s*\|")
_DIFF_HEADER = re.compile(r"^\s*[-+]\s?(?:Expected|Received|expected|actual)\b")
# The lookbehinds keep a long run of digits from being tried from every position (quadratic).
_COUNT = re.compile(r"(?<!\d)(\d+)\s+(failed|passed|skipped|todo|pending|total|errors?)\b")
_DURATION = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(ms|s|m|h|min)\b")
# DEFAULT: the first 30 non-empty lines of an error body are searched for the error type (jest
# prints a long preamble before the real `SyntaxError` of a suite that failed to run).
_ERROR_TYPE_SCAN = 30


class PreparedLines(tuple[str, ...]):
    """Cleaned lines that are already capped in length and stripped of trailing whitespace.

    Every parser regex relies on this: with no trailing whitespace and a bounded length, a
    pattern cannot take quadratic time on a hostile line. A `PreparedLines` passes through
    `prepare_lines` untouched, so the work is done once per log.
    """

    __slots__ = ()


def prepare_lines(lines: Sequence[str], limit: int) -> PreparedLines:
    """Cut each line at `limit` characters and strip its trailing whitespace (cached type)."""
    if isinstance(lines, PreparedLines):
        return lines
    return PreparedLines(x[:limit].rstrip() if len(x) > limit else x.rstrip() for x in lines)


def split_paren_suffix(text: str, max_inner: int = 40) -> tuple[str, str] | None:
    """`name (inner)` -> (`name`, `inner`) when `text` ends with a short parenthesis after a space.

    Done with `rfind` instead of a regex so a line of thousands of spaces or brackets cannot make
    the search quadratic.
    """
    text = text.rstrip()
    if not text.endswith(")"):
        return None
    idx = text.rfind("(", max(len(text) - max_inner - 2, 0))
    if idx <= 0 or not text[idx - 1].isspace():
        return None
    return text[:idx].rstrip(), text[idx + 1 : -1]


class Budget:
    """How many failure blocks a parser may still build (`parse.max_failures`).

    Building a failure parses its stack, so a log of a million failure blocks must not build them
    all. When the budget runs out the parser stops reading failures and the result is marked
    truncated.
    """

    __slots__ = ("exhausted", "remaining")

    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.exhausted = False

    def take(self) -> bool:
        """Use one unit; False (and `exhausted` set) when none is left."""
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True


@dataclass(frozen=True, slots=True)
class Run:
    """One test run in a log: the summary it printed and what was found in its own output.

    A log can hold several runs (retries, one runner command per package). Checking each run
    against its own summary is exact even when retries repeat the same failure, which a single
    log-wide count is not.
    """

    summary: Summary
    tests: int = 0  # failing test blocks found in this run
    suites: int = 0  # failing suites (files that could not run) found in this run
    files: int | None = None  # distinct files with a failure here; None if a failure had no file


@dataclass(frozen=True, slots=True)
class PartialParse:
    """What one framework parser found. `parse_log` merges these into a `ParsedLog`.

    `failures` are unique (one per test identity); `runs` keep the per-run counts. `stray` counts
    failed tests found outside any run that printed a summary (a log cut off before the summary).
    """

    results: tuple[CaseResult, ...] = ()
    failures: tuple[FailedTest, ...] = ()
    runs: tuple[Run, ...] = ()
    stray: int = 0
    granularity: Granularity = "none"
    warnings: tuple[str, ...] = ()
    truncated: bool = False  # the failure budget ran out: not every failure was read

    @property
    def summaries(self) -> tuple[Summary, ...]:
        """The summary of every run, in log order."""
        return tuple(r.summary for r in self.runs)


def make_run(summary: Summary, failures: Sequence[FailedTest], *, unique: bool = False) -> Run:
    """Count the failures found in one run's output next to the summary it printed.

    With `unique`, a test printed more than once counts once (vitest prints a block for every
    attempt of a retried test but counts the test once; mocha counts every failed attempt).
    """
    real = [f for f in failures if f.kind in ("test", "suite")]
    if unique:
        real = list({(f.kind, f.file, f.full_name): f for f in real}.values())
    tests = sum(1 for f in real if f.kind == "test")
    files = {f.file for f in real}
    return Run(
        summary=summary,
        tests=tests,
        suites=len(real) - tests,
        files=None if None in files else len(files),
    )


def duration_ms(value: str, unit: str) -> float:
    """`12`, `ms` -> 12.0; `1.5`, `s` -> 1500.0; `m` and `h` as minutes and hours."""
    factor = {"ms": 1.0, "s": 1000.0, "m": 60_000.0, "min": 60_000.0, "h": 3_600_000.0}[unit]
    return float(value) * factor


def last_duration_ms(text: str) -> float | None:
    """The last `<number><unit>` in `text` as milliseconds, or None."""
    found = _DURATION.findall(text)
    return duration_ms(*found[-1]) if found else None


def count_tokens(text: str) -> dict[str, int]:
    """`1 failed | 20 passed | 3 skipped (24)` -> {"failed": 1, "passed": 20, "skipped": 3}."""
    counts: dict[str, int] = {}
    for number, label in _COUNT.findall(text):
        key = "errors" if label.startswith("error") else label
        counts.setdefault(key, int(number))
    return counts


def merge_summaries(summaries: Iterable[Summary]) -> Summary | None:
    """Add up several summaries field by field; a field no summary printed stays None."""
    items = list(summaries)
    if not items:
        return None
    if len(items) == 1:
        return items[0]

    def total(values: list[int | None]) -> int | None:
        present = [v for v in values if v is not None]
        return sum(present) if present else None

    durations = [s.duration_ms for s in items if s.duration_ms is not None]
    return Summary(
        passed=total([s.passed for s in items]),
        failed=total([s.failed for s in items]),
        skipped=total([s.skipped for s in items]),
        todo=total([s.todo for s in items]),
        total=total([s.total for s in items]),
        files_failed=total([s.files_failed for s in items]),
        duration_ms=sum(durations) if durations else None,
    )


def dedent(lines: Sequence[str], amount: int) -> list[str]:
    """Remove up to `amount` leading spaces from each line."""
    out: list[str] = []
    for line in lines:
        stripped = len(line) - len(line.lstrip(" "))
        out.append(line[min(stripped, amount) :])
    return out


def find_error_type(lines: Sequence[str]) -> str | None:
    """The class name in the first `SomeError: ...` line near the top of an error body."""
    seen = 0
    for line in lines:
        if not line.strip():
            continue
        m = _ERROR_TYPE.match(line)
        if m:
            return m.group("type")
        seen += 1
        if seen >= _ERROR_TYPE_SCAN:
            break
    return None


def split_message(lines: Sequence[str], limit: int) -> tuple[str, str | None]:
    """Split a body into (message, diff); the message ends at the first stack or code frame."""
    end = len(lines)
    for i, line in enumerate(lines):
        if _CODE_FRAME.match(line) or parse_frame(line) is not None:
            end = i
            break
    head = list(lines[:end])
    diff: str | None = None
    for j, line in enumerate(head):
        if _DIFF_HEADER.match(line):
            diff = "\n".join(head[j:]).strip("\n")[:limit] or None
            head = head[:j]
            break
    return "\n".join(head).strip()[:limit], diff


def first_project_path(frames: Iterable[StackFrame], cfg: ParseSettings) -> str | None:
    """A test file's path from the stack: `*.test.*`, else a tests folder, else first project."""
    project = [f.path for f in frames if f.kind == "project" and f.path]
    for pattern in (cfg.test_file, cfg.test_path):
        for path in project:
            if pattern.search(path):
                return path
    return project[0] if project else None


def build_failure(
    name_parts: Sequence[str],
    body: Sequence[str],
    config: ParsingConfig,
    *,
    file: str | None = None,
    file_source: FileSource | None = None,
    kind: FailureKind = "test",
    hook: str | None = None,
    retries: int = 0,
) -> FailedTest:
    """Build a `FailedTest` from its name parts and the dedented lines of its error block."""
    cfg = config.parse
    lines = [line.rstrip() for line in body[: cfg.max_body_lines]]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    frames = tuple(parse_frames(lines, config))
    message, diff = split_message(lines, cfg.message_max_chars)
    if file is None:
        file = first_project_path(frames, cfg)
        file_source = "stack" if file else None
    parts = tuple(p for p in name_parts if p)
    return FailedTest(
        full_name=NAME_SEP.join(parts),
        name_parts=parts,
        file=file,
        file_source=file_source if file else None,
        error_type=find_error_type(lines),
        message=message,
        diff=diff,
        frames=frames,
        hook=hook,
        retries=retries,
        raw="\n".join(lines)[: cfg.failure_raw_max_chars],
        kind=kind,
    )


# DEFAULT: failures are unique per test identity (kind, file, name). The same test printed again
# (a retry, the same FAIL line twice) is one failure; the first print is kept, and it takes the
# other's file when it had none and the larger retry count. Two different tests that share a
# name and file (generated test names) collapse into one: `parse_log` then reports a mismatch.
def dedupe_failures(failures: Iterable[FailedTest], limit: int) -> tuple[list[FailedTest], bool]:
    """Drop repeats of the same failure (retries, the same test shown twice). Returns (list, cut).

    Two failures are the same when the name and kind match and the files match or one is unknown.
    The first one is kept; it takes the other's file when it had none, and the larger retry count.
    """
    kept: list[FailedTest] = []
    by_name: dict[tuple[str, str], list[int]] = {}
    cut = False
    for item in failures:
        slots = by_name.setdefault((item.kind, item.full_name), [])
        for index in slots:
            existing = kept[index]
            if existing.file == item.file or existing.file is None or item.file is None:
                update: dict[str, object] = {}
                if existing.file is None and item.file is not None:
                    update.update(file=item.file, file_source=item.file_source)
                if item.retries > existing.retries:
                    update["retries"] = item.retries
                if update:
                    kept[index] = replace(existing, **update)  # type: ignore[arg-type]
                break
        else:
            if len(kept) >= limit:
                cut = True
                continue
            slots.append(len(kept))
            kept.append(item)
    return kept, cut


# DEFAULT: one result per (file, name), and the last print wins, so a test that failed and then
# passed on retry shows as passed. The list keeps the order of the last prints.
def dedupe_results(results: Iterable[CaseResult], limit: int) -> tuple[list[CaseResult], bool]:
    """Keep one result per (file, name); the last one printed wins (a retry replaces its first)."""
    latest: dict[tuple[str | None, str], CaseResult] = {}
    for item in results:
        key = (item.file, item.full_name)
        if key in latest:
            del latest[key]  # move the key to the end so the order follows the last print
        latest[key] = item
    items = list(latest.values())
    if len(items) > limit:
        return items[:limit], True
    return items, False
