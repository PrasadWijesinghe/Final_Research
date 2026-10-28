"""Parser for Vitest output (default, verbose, dot and progress reporters).

What it reads, in the cleaned lines of one log:
- Summaries: the ` Test Files  1 failed | 23 passed (24)` and `Tests  1 failed | 621 passed` rows,
  anchored to vitest's own column alignment so a nested vitest run printed inside an error is not
  counted. Windows runs interleave these lines with error text; they are filtered out of bodies.
- Failures: the `⎯⎯⎯ Failed Tests N ⎯⎯⎯` section (` FAIL  [project]  file > suite > test` lines
  sharing one error body, ended by `⎯⎯⎯[1/2]⎯`), the `Failed Suites` section (a file that could not
  load, `kind="suite"`) and `Unhandled Errors` (`kind="unhandled"`).
- Results: ` ✓ file (N tests) 12ms` file lines, and the nested `✓`/`×`/`↓` test lines of the
  verbose reporter. The dot reporter prints neither, so only failures are read for it.

A `(retry xN)` suffix is removed from names and kept in `retries`.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import replace

from testing_expert.parsing._common import (
    Budget,
    PartialParse,
    build_failure,
    count_tokens,
    dedupe_failures,
    dedupe_results,
    duration_ms,
    last_duration_ms,
    make_run,
    prepare_lines,
    split_paren_suffix,
)
from testing_expert.parsing.models import (
    CaseResult,
    FailedTest,
    FailureKind,
    Granularity,
    Outcome,
    Summary,
)
from testing_expert.parsing.settings import ParsingConfig, load_config
from testing_expert.parsing.stack import map_path

SUITE_FAILED_NAME = "Test suite failed to run"

# Lines reach these patterns right-stripped and capped (see `prepare_lines`), so none of them ends
# in `\s*$`: with a lazy `.*?` before it that is quadratic on a line of spaces (and a lazy title
# between two `\s*` was cubic).
_FILES = re.compile(r"^ {1,2}Test Files\s+(?P<rest>\d.*)$")
_TESTS = re.compile(r"^ {4,8}Tests\s+(?P<rest>\d.*)$")
_DURATION_LINE = re.compile(r"^ {1,8}Duration\s+(?P<n>\d+(?:\.\d+)?)(?P<u>ms|s|m)\b")
# DEFAULT: a Tests row is paired with a Test Files row up to this many lines above it.
_PAIR_WINDOW = 300
_NOISE_LINE = re.compile(
    r"^ {0,8}(?:Test Files|Tests|Type Errors|Errors|Start at|Duration)\s{2,}\S"
)  # summary rows that Windows runs interleave with error text
_SECTION = re.compile(r"^⎯{3,}(?P<title>[^⎯]+)⎯{3,}$")  # the title is stripped in code
_SEP = re.compile(r"^⎯{3,}\[\d+/\d+\]⎯*$")
_RULE = re.compile(r"^⎯{6,}$")
_FAIL = re.compile(r"^ FAIL\s+(?P<rest>\S.*)$")
_CUT = re.compile(r"^\[(?:fixture trimmed|log truncated): \d+ lines omitted\]$")
_WATCH_NOTE = re.compile(r"^Tests? failed\. Watching for file changes")
_BROWSER_BADGE = re.compile(r"^\|[^|\s]+\|\s*")
_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
_FILE_HEAD = re.compile(r"^ (?P<mark>[✓❯×↓✗])\s+")
_FILE_COUNTS = re.compile(r"\(\d+ tests?(?: \| \d+ (?:failed|skipped|todo))*\)")
_NESTED = re.compile(r"^(?P<indent>\s{2,})(?P<mark>[✓×↓])\s+(?P<text>\S.*)$")
_VERBOSE = re.compile(r"^ {1,2}(?P<mark>[✓×↓])\s+(?P<text>\S.*)$")
_TEST_DURATION = re.compile(r"(?P<n>\d+(?:\.\d+)?)(?P<u>ms|s|m)")
_RETRY_RESULT = re.compile(r"^\s+×\s+(?P<text>\S.*)$")
_PATHISH = re.compile(r"[/\\]|\.\w{1,5}(?::\d+)?$")
_UNHANDLED_TITLES = ("Unhandled", "Uncaught")
_RETRY_PREFIX = "retry x"
_MARK_OUTCOME: dict[str, Outcome] = {
    "✓": "passed",
    "❯": "failed",
    "×": "failed",
    "✗": "failed",
    "↓": "skipped",
}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return not line.strip()


def _section_title(line: str) -> str | None:
    """The title of a `⎯⎯⎯ Title ⎯⎯⎯` rule, or None (the `⎯⎯⎯[1/2]⎯` separator is not one)."""
    m = _SECTION.match(line)
    title = m.group("title").strip() if m else ""
    return title or None


def _pop_retry(text: str) -> tuple[str, int]:
    """`name (retry x2)` -> (`name`, 2); the text unchanged and 0 when there is no such suffix."""
    paren = split_paren_suffix(text)
    if paren is not None and paren[1].startswith(_RETRY_PREFIX):
        digits = paren[1][len(_RETRY_PREFIX) :]
        if digits.isdigit():
            return paren[0], int(digits)
    return text, 0


def _file_from(text: str, config: ParsingConfig) -> str:
    """The file in `[project  ]file[:line]`: the last block of text, without a line suffix.

    The project badge is `name` followed by two spaces, or `|browser|` followed by one.
    """
    token = _BROWSER_BADGE.sub("", re.split(r"\s{2,}", text.strip())[-1])
    token = _LINE_SUFFIX.sub("", token)
    mapped, _kind = map_path(token, config)
    return mapped or token


def _total_of(rest: str) -> int | None:
    """The `(719)` at the end of `1 failed | 717 passed (719)`."""
    if rest.endswith(")"):
        cut = rest.rfind("(")
        if cut >= 0 and rest[cut + 1 : -1].isdigit():
            return int(rest[cut + 1 : -1])
    return None


def _summaries(lines: Sequence[str]) -> list[tuple[int, Summary]]:
    """(line index of the ` Test Files` row, Summary) for each run, from the summary rows."""
    files: list[tuple[int, dict[str, int]]] = []
    tests: list[tuple[int, dict[str, int], str]] = []
    for i, line in enumerate(lines):
        if not line.startswith(" "):
            continue
        m = _FILES.match(line)
        if m:
            files.append((i, count_tokens(m.group("rest"))))
            continue
        m = _TESTS.match(line)
        if m:
            tests.append((i, count_tokens(m.group("rest")), m.group("rest")))

    def duration_after(index: int) -> float | None:
        for line in lines[index + 1 : index + 8]:
            m = _DURATION_LINE.match(line)
            if m:
                return duration_ms(m.group("n"), m.group("u"))
        return None

    used: set[int] = set()
    out: list[tuple[int, Summary]] = []
    # A Tests row pairs with the nearest unused Test Files row above it (Windows runs interleave
    # the two with error text, so they can be far apart). Both lists are in line order: the Files
    # rows above the current Tests row wait on a stack, and the top of the stack is the nearest.
    # A Files row beyond the window of this Tests row is beyond it for every later one too.
    waiting: list[int] = []
    next_file = 0
    for t_index, counts, rest in tests:
        while next_file < len(files) and files[next_file][0] < t_index:
            waiting.append(next_file)
            next_file += 1
        pair = waiting[-1] if waiting and t_index - files[waiting[-1]][0] <= _PAIR_WINDOW else None
        files_failed = None
        anchor = t_index
        if pair is not None:
            waiting.pop()
            used.add(pair)
            files_failed = files[pair][1].get("failed", 0)
            anchor = files[pair][0]
        out.append(
            (
                anchor,
                Summary(
                    passed=counts.get("passed", 0),
                    failed=counts.get("failed", 0),
                    skipped=counts.get("skipped", 0),
                    todo=counts.get("todo", 0),
                    total=_total_of(rest),
                    files_failed=files_failed,
                    duration_ms=duration_after(t_index),
                ),
            )
        )
    for k, (f_index, counts) in enumerate(files):
        if k not in used:  # no `Tests` row: every file failed to load, or the log was cut
            out.append(
                (
                    f_index,
                    Summary(
                        files_failed=counts.get("failed", 0), duration_ms=duration_after(f_index)
                    ),
                )
            )
    out.sort(key=lambda item: item[0])
    return out


def _name_parts(
    rest: str, config: ParsingConfig, section: str | None
) -> tuple[str, list[str], int, FailureKind]:
    """Split the text after ` FAIL ` into (file, name parts, retries, kind).

    Under `Failed Suites` every entry is a suite failure, with the describe name when there is one.
    """
    rest, retries = _pop_retry(rest)
    kind: FailureKind = "suite" if section == "suites" else "test"
    if " > " in rest:
        left, _, right = rest.partition(" > ")
        return _file_from(left, config), [p.strip() for p in right.split(" > ")], retries, kind
    left = rest
    if left.endswith("]"):  # `file [ file ]`: a failed suite lists its file twice
        cut = left.rfind(" [")
        if cut > 0:
            left = left[:cut]
    return _file_from(left, config), [SUITE_FAILED_NAME], retries, "suite"


def _failures(
    lines: Sequence[str], config: ParsingConfig, budget: Budget
) -> list[tuple[int, bool, FailedTest]]:
    """Every failure with (anchor, after_summary): where it is, and in which order it came.

    The anchor is the line of its section header (or of its own FAIL line). The summary rows come
    after the failure sections, so the anchor tells which run a failure belongs to, except in
    watch mode, where vitest prints the summary first and then the failures (`after_summary`).
    Stops reading when the budget runs out.
    """
    n = len(lines)
    limit = config.parse.max_body_lines
    found: list[tuple[int, bool, FailedTest]] = []
    section: str | None = None
    section_at = 0
    # DEFAULT: in watch mode (`Tests failed. Watching for file changes...`) vitest prints a run's
    # summary rows before its failure sections, so those failures belong to the summary above.
    after_summary = False
    i = 0
    while i < n:
        line = lines[i]
        title = _section_title(line) if line.startswith("⎯") else None
        if title is not None:
            if title.startswith("Failed Tests"):
                section, section_at = "tests", i
            elif title.startswith("Failed Suites"):
                section, section_at = "suites", i
            elif title.startswith("Unhandled Errors"):
                section, section_at = "unhandled", i
            elif section == "unhandled" and title.startswith(_UNHANDLED_TITLES):
                end = i + 1
                while end < n and end - i < limit:
                    cand = lines[end]
                    if cand.startswith("⎯") and (
                        _section_title(cand) is not None or _SEP.match(cand) or _RULE.match(cand)
                    ):
                        break
                    if _FILES.match(cand) or _CUT.match(cand):
                        break
                    end += 1
                body = [x for x in lines[i + 1 : end] if not _NOISE_LINE.match(x)]
                first = next((x.strip() for x in body if x.strip()), "")
                if not budget.take():
                    break
                # DEFAULT: an unhandled error is named `<section title> > <first error line>`
                # (cut at 120 characters), so different errors stay different.
                found.append(
                    (
                        section_at,
                        after_summary,
                        build_failure([title, first[:120]], body, config, kind="unhandled"),
                    )
                )
                i = end
                continue
            else:
                section = None
            i += 1
            continue
        m = _FAIL.match(line)
        if m and _WATCH_NOTE.match(m.group("rest")):
            after_summary = True  # watch mode: this run's summary is already above
            i += 1
            continue
        if _FILES.match(line):
            after_summary = False
        if m and (section in ("tests", "suites") or " > " in m.group("rest")):
            anchor = section_at if section in ("tests", "suites") else i
            group: list[str] = []
            j = i
            while j < n:
                mm = _FAIL.match(lines[j])
                if mm:
                    group.append(mm.group("rest"))
                    j += 1
                elif _is_blank(lines[j]) or _NOISE_LINE.match(lines[j]):
                    j += 1
                else:
                    break
            k = j
            while k < n and k - j < limit:
                cand = lines[k]
                if cand.startswith("⎯") and (
                    _SEP.match(cand) or _section_title(cand) is not None or _RULE.match(cand)
                ):
                    break
                if _FAIL.match(cand) or _CUT.match(cand):
                    break
                k += 1
            body = [x for x in lines[j:k] if not _NOISE_LINE.match(x)]
            seen: set[tuple[str, str]] = set()  # the same FAIL line twice in a group is one failure
            for rest in group:
                file, parts, retries, kind = _name_parts(rest, config, section)
                key = (file, " > ".join(parts))
                if key in seen:
                    continue
                seen.add(key)
                if not budget.take():
                    return found
                found.append(
                    (
                        anchor,
                        after_summary,
                        build_failure(
                            parts,
                            body,
                            config,
                            file=file,
                            file_source="header",
                            kind=kind,
                            retries=retries,
                        ),
                    )
                )
            i = max(k, i + 1)
            continue
        i += 1
    return found


def _split_test_text(text: str) -> tuple[str, float | None, bool]:
    """`name 12ms (retry x1)` -> (name, 12.0, is_suite_line); `describe (7)` is a suite line."""
    text, _retries = _pop_retry(text.rstrip())
    head, sep, last = text.rpartition(" ")
    if sep:
        m = _TEST_DURATION.fullmatch(last)
        if m:
            return head.rstrip(), duration_ms(m.group("n"), m.group("u")), False
    paren = split_paren_suffix(text)
    if paren is not None and paren[1].isdigit():
        return paren[0], None, True
    return text, None, False


def _results(lines: Sequence[str], config: ParsingConfig) -> tuple[list[CaseResult], Granularity]:
    per_test: list[CaseResult] = []
    per_file: list[CaseResult] = []
    complete = False  # the verbose reporter prints every test; the default one only some
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        head = _FILE_HEAD.match(line) if line.startswith(" ") else None
        counts = _FILE_COUNTS.search(line, head.end()) if head else None
        named = (
            head is not None
            and counts is not None
            and line[counts.start() - 1].isspace()
            and bool(line[head.end() : counts.start()].strip())
        )
        if not named or head is None or counts is None:
            v = _VERBOSE.match(line) if line.startswith(" ") else None
            if v and " > " in v.group("text"):  # `✓ file > suite > test 12ms` (verbose reporter)
                name, dur, _suite = _split_test_text(v.group("text"))
                left, _, right = name.partition(" > ")
                if _PATHISH.search(re.split(r"\s{2,}", left.strip())[-1]):
                    complete = True
                    file = _file_from(left, config)
                    per_test.append(CaseResult(right, file, _MARK_OUTCOME[v.group("mark")], dur))
            i += 1
            continue
        file = _file_from(line[head.end() : counts.start()], config)
        outcome = _MARK_OUTCOME[head.group("mark")]
        per_file.append(CaseResult(file, file, outcome, last_duration_ms(line[counts.end() :])))
        stack: list[tuple[int, str]] = []
        j = i + 1
        while j < n:
            nested = _NESTED.match(lines[j])
            if nested is None:
                break
            indent = len(nested.group("indent"))
            name, dur, is_suite = _split_test_text(nested.group("text"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if is_suite:
                complete = True  # only the verbose reporter prints `describe (N)` lines
                stack.append((indent, name))
            else:
                parents = [t for _ind, t in stack]
                per_test.append(
                    CaseResult(
                        " > ".join([*parents, name]), file, _MARK_OUTCOME[nested.group("mark")], dur
                    )
                )
            j += 1
        i = max(j, i + 1)
    # DEFAULT: a file with test lines is represented by them; only files without test lines get a
    # file-level row (`name` and `file` are both the path).
    tested = {r.file for r in per_test}
    results = per_test + [r for r in per_file if r.file not in tested]
    # DEFAULT: "test" only when the reporter printed every test (verbose). The default reporter
    # lists just the failing and slow tests under their file line; those lines are kept in the
    # results, but the granularity stays "file" (or "none" when there is no file line either).
    granularity: Granularity = "test" if complete else "file" if per_file else "none"
    return results, granularity


def parse(lines: Sequence[str], config: ParsingConfig | None = None) -> PartialParse:
    """Parse Vitest output lines into failures, results and runs. Never raises on odd text."""
    cfg = config or load_config()
    lines = prepare_lines(lines, cfg.parse.max_line_chars)
    summaries = _summaries(lines)
    budget = Budget(cfg.parse.max_failures)
    anchored = _failures(lines, cfg, budget)
    anchors = [at for at, _s in summaries]  # in line order
    per_run: list[list[FailedTest]] = [[] for _ in summaries]
    stray = 0
    for anchor, after_summary, failure in anchored:
        if after_summary:  # watch mode: the nearest summary above
            index = bisect_left(anchors, anchor) - 1
            index = index if index >= 0 else len(summaries)
        else:  # the nearest summary below
            index = bisect_right(anchors, anchor)
        if index >= len(summaries):
            stray += 1 if failure.kind == "test" else 0
        else:
            per_run[index].append(failure)
    runs = tuple(make_run(s, per_run[k], unique=True) for k, (_at, s) in enumerate(summaries))
    kept, cut = dedupe_failures((f for _at, _after, f in anchored), cfg.parse.max_failures)
    kept = _with_retries(kept, lines)
    results, granularity = _results(lines, cfg)
    results = _name_failed_results(results, kept)
    results, results_cut = dedupe_results(results, cfg.parse.max_results)
    warnings: list[str] = []
    if budget.exhausted or cut:
        warnings.append(
            f"vitest: stopped after {cfg.parse.max_failures} failure blocks; the rest are not read"
        )
    if results_cut:
        warnings.append(f"vitest: more than {cfg.parse.max_results} results; the rest are dropped")
    return PartialParse(
        results=tuple(results),
        failures=tuple(kept),
        runs=runs,
        stray=stray,
        granularity=granularity,
        warnings=tuple(warnings),
        truncated=budget.exhausted or cut,
    )


def _with_retries(failures: list[FailedTest], lines: Sequence[str]) -> list[FailedTest]:
    """Take `retries` from the `× name 21ms (retry x1)` result lines (the FAIL line has none)."""
    retries: dict[str, int] = {}
    for line in lines:
        if "(retry x" not in line:
            continue
        m = _RETRY_RESULT.match(line)
        if m:
            text, count = _pop_retry(m.group("text"))
            if count:
                name, _dur, _suite = _split_test_text(text)
                retries[name] = max(retries.get(name, 0), count)
    if not retries:
        return failures
    return [
        replace(f, retries=max(f.retries, retries.get(f.name_parts[-1], 0))) if f.name_parts else f
        for f in failures
    ]


def _name_failed_results(results: list[CaseResult], failures: list[FailedTest]) -> list[CaseResult]:
    """In the default reporter a test line carries no describe names; a failed one takes the full
    name its FAIL block printed, so the same test has the same name everywhere."""
    by_key = {(f.file, f.name_parts[-1]): f.full_name for f in failures if f.name_parts}
    out: list[CaseResult] = []
    for r in results:
        full = by_key.get((r.file, r.full_name)) if r.outcome == "failed" else None
        out.append(replace(r, full_name=full) if full else r)
    return out
