"""Parser for Node's built-in test runner (`node --test`, `node:test`): TAP and spec reporters.

Node picks the reporter itself: spec when stdout is a terminal, TAP when it is not, which is the
CI case. Both are read from the same cleaned lines, so a log that holds either (or both) works.

TAP reporter:
- `# Subtest: name` lines (indented four columns per nesting level) give the describe names.
- `ok N - name` / `not ok N - name`, followed by a YAML block (`---` ... `...`) with
  `duration_ms`, `location`, `failureType`, `error`, `code`, `name` (the error class), `stack`.
  The stack has no `at ` prefix; one is added so the shared stack-frame parser reads it.
- `# tests N`, `# pass N`, `# fail N`, `# cancelled N`, `# skipped N`, `# todo N`: the summary.

Spec reporter:
- `▶ suite` opens a suite, `✔ name (12ms)`, `✖ name (12ms)` and `﹣ name (0ms) # SKIP` are tests,
  and a suite is closed by a `✔`/`✖` line with the suite's own name. Names are rebuilt from the
  indentation. The errors are printed once more in the `✖ failing tests:` section, each with a
  `test at file:line:col` line first.
- `ℹ tests N`, `ℹ pass N`, ... is the summary.

A parent suite whose child failed is reported too (`failureType: 'subtestsFailed'`, "1 subtest
failed"); it is skipped so one failure is not counted twice. A test cancelled because its parent
ended (`cancelledByParent`) is collateral damage of another failure and is skipped as well, unless
the run holds no other failure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from testing_expert.parsing._common import (
    NAME_SEP,
    Budget,
    PartialParse,
    Run,
    build_failure,
    dedupe_failures,
    dedupe_results,
    last_duration_ms,
    prepare_lines,
    split_paren_suffix,
)
from testing_expert.parsing.models import CaseResult, FailedTest, Granularity, Outcome, Summary
from testing_expert.parsing.settings import ParsingConfig, load_config
from testing_expert.parsing.stack import map_path

# Lines reach these patterns right-stripped and capped (see `prepare_lines`), so none of them ends
# in `\s*$`.
_SUMMARY = re.compile(
    r"^(?:#|ℹ) (?P<key>tests|suites|pass|fail|cancelled|skipped|todo|duration_ms) "
    r"(?P<n>\d+(?:\.\d+)?)$"
)
_SUBTEST = re.compile(r"^(?P<indent> *)# Subtest: (?P<name>\S.*)$")
_RESULT = re.compile(r"^(?P<indent> *)(?P<ok>not ok|ok) (?P<n>\d+)(?: - (?P<rest>.*))?$")
_BLOCK_OPEN = re.compile(r"^(?P<indent> +)---$")
_KEY = re.compile(r"^(?P<key>[A-Za-z_]\w*):(?: (?P<val>.*))?$")
_LITERAL_MARKS = frozenset({"|", "|-", "|+", ">", ">-", ">+"})
_SPEC_LINE = re.compile(r"^(?P<indent> *)(?P<mark>[▶✔✖﹣])\s+(?P<text>\S.*)$")
_FAILING_HEADER = re.compile(r"^✖ failing tests:$")
_LOCATION_LINE = re.compile(r"^(?P<indent> *)(?:test at )?(?P<loc>\S.*:\d+:\d+)$")
_LOC_SUFFIX = re.compile(r":\d+(?::\d+)?$")
_ERROR_HEAD = re.compile(r"^(?:[A-Z][\w$]*)?(?:Error|Exception)\b")
_SUBTESTS_FAILED = re.compile(r"^\d+ subtests? failed$")
_CANCELLED_TEXT = "test did not finish before its parent and was cancelled"
_STACK_PREFIX = "at "
_CUT = re.compile(r"^\[(?:fixture trimmed|log truncated): \d+ lines omitted\]$")
# DEFAULT: a failing test's error block is walked for at most this many lines per block before
# the walk gives up looking for the closing `...` (a block that never closes is malformed).
_MAX_BLOCK_WALK = 2000


@dataclass(frozen=True, slots=True)
class _Entry:
    """One test as printed, before it is assigned to a run."""

    index: int
    result: CaseResult | None
    failure: FailedTest | None = None
    collateral: bool = False  # cancelled by its parent: not a failure of its own
    after_summary: bool = False  # printed after the summary (spec `failing tests:` section)


@dataclass(slots=True)
class _Group:
    """One summary the log printed and the lines it covers."""

    start: int
    end: int
    summary: Summary
    entries: list[_Entry] = field(default_factory=list)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value[1:-1]
    return value


def _unescape_name(name: str) -> str:
    """TAP escapes `#` and `\\` in test names."""
    return name.replace("\\#", "#").replace("\\\\", "\\")


def _split_directive(text: str) -> tuple[str, Outcome | None]:
    """`name # SKIP why` -> (`name`, "skipped"); `name # TODO` -> (`name`, "todo")."""
    head, sep, tail = text.rpartition(" # ")
    if sep:
        word = tail.strip().upper()
        if word.startswith("SKIP"):
            return head.rstrip(), "skipped"
        if word.startswith("TODO"):
            return head.rstrip(), "todo"
    return text, None


def _file_from_location(loc: str | None, config: ParsingConfig) -> str | None:
    """Repo-relative path of a YAML `location:` (Windows path, POSIX path or `file:///` URL)."""
    if not loc:
        return None
    path = _LOC_SUFFIX.sub("", loc.strip())
    path = path.replace("\\\\", "\\")  # the YAML printer doubles every backslash
    mapped, kind = map_path(path, config)
    return mapped if kind == "project" else None


def _frame_lines(stack: Sequence[str]) -> list[str]:
    """TAP stacks hold bare frames (`fn (loc)`); add the `at ` the frame parser expects."""
    out: list[str] = []
    for line in stack:
        text = line.strip()
        if not text:
            continue
        out.append(f"    {text}" if text.startswith(_STACK_PREFIX) else f"    at {text}")
    return out


def _body(fields: dict[str, str], error: Sequence[str], stack: Sequence[str]) -> list[str]:
    """The error text as other parsers see it: `Type [CODE]: message`, then the frames."""
    lines = [x.rstrip() for x in error]
    while lines and not lines[0]:
        lines.pop(0)
    type_name = fields.get("name")
    code = fields.get("code")
    if lines and type_name and not _ERROR_HEAD.match(lines[0]):
        lines[0] = f"{type_name}{f' [{code}]' if code else ''}: {lines[0]}"
    elif not lines and type_name:
        lines = [f"{type_name}{f' [{code}]' if code else ''}"]
    return [*lines, *_frame_lines(stack)]


def _build(
    parts: Sequence[str],
    fields: dict[str, str],
    error: Sequence[str],
    stack: Sequence[str],
    config: ParsingConfig,
    budget: Budget,
) -> FailedTest | None:
    """Build one `FailedTest` (None when the failure budget is spent)."""
    if not budget.take():
        return None
    file = _file_from_location(fields.get("location"), config)
    failure = build_failure(
        parts,
        _body(fields, error, stack),
        config,
        file=file,
        file_source="header" if file else None,
    )
    expected, actual = fields.get("expected"), fields.get("actual")
    if failure.diff is None and expected is not None and actual is not None:
        diff = f"expected: {expected}\nactual: {actual}"[: config.parse.message_max_chars]
        failure = replace(failure, diff=diff)
    return failure


def _finish(
    fields: dict[str, str], literals: dict[str, list[str]], end: int
) -> tuple[dict[str, str], list[str], list[str], int]:
    """Error and stack as lines: a one-line `error: 'text'` is a scalar, not a literal block."""
    for key in ("error", "stack"):
        if key not in literals and fields.get(key):
            literals[key] = [fields[key]]
    return fields, literals.get("error", []), literals.get("stack", []), end


def _read_block(
    lines: Sequence[str], start: int, result_indent: int, max_lines: int
) -> tuple[dict[str, str], list[str], list[str], int]:
    """Read the YAML block that follows a TAP result line.

    Returns (scalar fields, error lines, stack lines, next index). `start` is the line after the
    result; when it is not a `---` opener nothing is read and `start` comes back.
    """
    n = len(lines)
    if start >= n:
        return {}, [], [], start
    opener = _BLOCK_OPEN.match(lines[start])
    if not opener or len(opener.group("indent")) <= result_indent:
        return {}, [], [], start
    base = len(opener.group("indent"))
    closer = " " * base + "..."
    fields: dict[str, str] = {}
    literals: dict[str, list[str]] = {}
    j = start + 1
    limit = min(n, start + _MAX_BLOCK_WALK)
    while j < limit:
        line = lines[j]
        if line == closer:
            return _finish(fields, literals, j + 1)
        if line.strip() and len(line) - len(line.lstrip(" ")) < base:
            break  # malformed: the block was never closed
        if _CUT.match(line.strip()):
            break
        m = _KEY.match(line[base:]) if line.startswith(" " * base) else None
        if m is None or line[base : base + 1] == " ":
            j += 1
            continue
        key, val = m.group("key"), m.group("val") or ""
        if val.strip() in _LITERAL_MARKS:
            collected: list[str] = []
            k = j + 1
            while k < limit:
                nxt = lines[k]
                if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= base:
                    break
                if len(collected) < max_lines:
                    collected.append(nxt[base + 2 :] if len(nxt) > base + 2 else nxt.strip())
                k += 1
            literals[key] = collected
            j = k
            continue
        fields[key] = _unquote(val)
        j += 1
    return _finish(fields, literals, j)


def _scan_tap(
    lines: Sequence[str], config: ParsingConfig, budget: Budget
) -> tuple[list[_Entry], list[str]]:
    """Read every `ok` / `not ok` entry of the TAP reporter."""
    entries: list[_Entry] = []
    warnings: list[str] = []
    stack: list[tuple[int, str]] = []
    n = len(lines)
    max_lines = config.parse.max_body_lines
    i = 0
    while i < n:
        line = lines[i]
        i += 1
        sub = _SUBTEST.match(line)
        if sub:
            indent = len(sub.group("indent"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, _unescape_name(sub.group("name"))))
            continue
        m = _RESULT.match(line)
        if not m:
            continue
        indent = len(m.group("indent"))
        at = i - 1
        name, directive = _split_directive(_unescape_name((m.group("rest") or "").strip()))
        parents = [t for ind, t in stack if ind < indent]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        fields, error, frames, i = _read_block(lines, i, indent, max_lines)
        if not name:
            continue
        parts = [*parents, name]
        full = NAME_SEP.join(parts)
        failure_type = fields.get("failureType", "")
        if fields.get("type") == "suite" and (
            m.group("ok") == "ok" or failure_type == "subtestsFailed"
        ):
            continue  # a suite is not a test; a failing one only repeats its failing children
        if failure_type == "subtestsFailed":
            continue
        duration = None
        if "duration_ms" in fields:
            try:
                duration = float(fields["duration_ms"])
            except ValueError:
                duration = None
        file = _file_from_location(fields.get("location"), config)
        if directive is not None:
            entries.append(_Entry(at, CaseResult(full, file, directive, duration)))
        elif m.group("ok") == "ok":
            entries.append(_Entry(at, CaseResult(full, file, "passed", duration)))
        else:
            collateral = failure_type == "cancelledByParent" or (
                not failure_type and " ".join(error).strip() == _CANCELLED_TEXT
            )
            failure = _build(parts, fields, error, frames, config, budget)
            entries.append(
                _Entry(at, CaseResult(full, file, "failed", duration), failure, collateral)
            )
    return entries, warnings


def _duration_of(text: str) -> tuple[str, float | None]:
    """`name (12ms)` -> (`name`, 12.0)."""
    paren = split_paren_suffix(text)
    if paren is None:
        return text, None
    ms = last_duration_ms(paren[1])
    return (paren[0], ms) if ms is not None else (text, None)


@dataclass(frozen=True, slots=True)
class _SpecFail:
    index: int
    parts: tuple[str, ...]
    duration: float | None


def _scan_spec_tree(
    lines: Sequence[str], end: int
) -> tuple[list[_Entry], list[_SpecFail], set[str]]:
    """Read the `▶` / `✔` / `✖` tree before the `failing tests:` section (lines `[0, end)`).

    Returns the results, the failed leaf tests (to pair with the section's errors) and the names
    of every suite seen.
    """
    entries: list[_Entry] = []
    failed: list[_SpecFail] = []
    suites: set[str] = set()
    stack: list[tuple[int, str]] = []
    for at in range(end):
        m = _SPEC_LINE.match(lines[at])
        if not m:
            continue
        indent = len(m.group("indent"))
        mark = m.group("mark")
        text = m.group("text")
        if mark == "✖" and text == "failing tests:":
            continue
        if mark == "▶":
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, text))
            suites.add(text)
            continue
        text, directive = _split_directive(text)
        name, duration = _duration_of(text)
        if stack and stack[-1][0] == indent and stack[-1][1] == name:
            stack.pop()  # the closing line of a suite
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parts = tuple([*(t for _, t in stack), name])
        outcome: Outcome = (
            directive if directive else {"✔": "passed", "✖": "failed", "﹣": "skipped"}[mark]  # type: ignore[assignment]
        )
        entries.append(_Entry(at, CaseResult(NAME_SEP.join(parts), None, outcome, duration)))
        if outcome == "failed":
            failed.append(_SpecFail(at, parts, duration))
    return entries, failed, suites


def _scan_spec_section(
    lines: Sequence[str],
    start: int,
    failed: list[_SpecFail],
    suites: set[str],
    config: ParsingConfig,
    budget: Budget,
) -> list[_Entry]:
    """Read the errors under `✖ failing tests:` and pair each with its test from the tree."""
    entries: list[_Entry] = []
    n = len(lines)
    used: set[int] = set()
    leaf_names = {f.parts[-1] for f in failed}
    max_lines = config.parse.max_body_lines
    i = start
    while i < n and not lines[i].strip():
        i += 1
    while i + 1 < n:
        loc = _LOCATION_LINE.match(lines[i])
        head = _SPEC_LINE.match(lines[i + 1])
        if (
            not loc
            or not head
            or head.group("mark") != "✖"
            or len(loc.group("indent")) != len(head.group("indent"))
        ):
            break
        indent = len(head.group("indent"))
        at = i + 1
        i += 2
        body: list[str] = []
        while i < n:
            line = lines[i]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
                break
            if len(body) < max_lines:
                body.append(line[indent + 2 :] if len(line) > indent + 2 else line.strip())
            i += 1
        while i < n and not lines[i].strip():
            i += 1
        name, directive = _split_directive(head.group("text"))
        name, duration = _duration_of(name)
        while body and not body[-1].strip():
            body.pop()
        while body and not body[0].strip():
            body.pop(0)
        first = body[0].strip() if body else ""
        if directive is not None or _SUBTESTS_FAILED.match(first):
            continue
        if name in suites and name not in leaf_names:
            continue
        match = next((f for f in failed if f.index not in used and f.parts[-1] == name), None)
        if match is not None:
            used.add(match.index)
        parts = match.parts if match else (name,)
        file = _file_from_location(loc.group("loc").replace("\\\\", "\\"), config)
        failure = None
        if budget.take():
            failure = build_failure(
                parts,
                body,
                config,
                file=file,
                file_source="header" if file else None,
            )
        collateral = first == _CANCELLED_TEXT
        entries.append(
            _Entry(
                at,
                # the tree already holds this test's result; only a test it never showed adds one
                None if match else CaseResult(NAME_SEP.join(parts), file, "failed", duration),
                failure,
                collateral,
                after_summary=True,
            )
        )
    # Failed tests the section did not describe (a cut log): keep them as failures with no text.
    for item in failed:
        if item.index in used:
            continue
        if any(e.failure and e.failure.name_parts == item.parts for e in entries):
            continue
        if budget.take():
            entries.append(
                _Entry(
                    item.index,
                    None,
                    build_failure(item.parts, [], config),
                )
            )
    return entries


def _scan_summaries(lines: Sequence[str]) -> list[_Group]:
    """Find each block of `# tests N` ... (or `ℹ tests N` ...) lines."""
    groups: list[_Group] = []
    n = len(lines)
    i = 0
    while i < n:
        m = _SUMMARY.match(lines[i])
        if not m or m.group("key") != "tests":
            i += 1
            continue
        start = i
        counts: dict[str, float] = {}
        while i < n:
            m = _SUMMARY.match(lines[i])
            if not m:
                break
            counts.setdefault(m.group("key"), float(m.group("n")))
            i += 1
        passed = int(counts.get("pass", 0))
        # DEFAULT: Node counts a test that timed out or was cancelled under `cancelled`, not
        # `fail`, yet the run fails, so failed = fail + cancelled.
        failed = int(counts.get("fail", 0)) + int(counts.get("cancelled", 0))
        groups.append(
            _Group(
                start,
                i,
                Summary(
                    passed=passed,
                    failed=failed,
                    skipped=int(counts.get("skipped", 0)),
                    todo=int(counts.get("todo", 0)),
                    total=int(counts["tests"]),
                    duration_ms=counts.get("duration_ms"),
                ),
            )
        )
    return groups


def _assign(groups: list[_Group], entries: list[_Entry]) -> list[_Entry]:
    """Give each entry to the run it belongs to; returns the entries after the last summary."""
    stray: list[_Entry] = []
    for entry in entries:
        target: _Group | None = None
        if entry.after_summary:
            target = next((g for g in reversed(groups) if g.end <= entry.index), None)
        else:
            target = next((g for g in groups if g.start >= entry.index), None)
        if target is None:
            stray.append(entry)
        else:
            target.entries.append(entry)
    return stray


def _failures_of(entries: Sequence[_Entry]) -> tuple[list[FailedTest], int]:
    """The failures to report for one run, and how many collateral ones were left out.

    DEFAULT: a test cancelled by its parent is dropped when the run has another failure (it only
    says "the parent ended"); a run with nothing else keeps them, so the log does not read as clean.
    """
    real = [e.failure for e in entries if e.failure and not e.collateral]
    collateral = [e.failure for e in entries if e.failure and e.collateral]
    if real:
        return real, len(collateral)
    return collateral, 0


def parse(lines: Sequence[str], config: ParsingConfig | None = None) -> PartialParse:
    """Parse `node --test` output (TAP or spec) into failures, results and runs. Never raises."""
    cfg = config or load_config()
    lines = prepare_lines(lines, cfg.parse.max_line_chars)
    budget = Budget(cfg.parse.max_failures)
    groups = _scan_summaries(lines)
    entries, warnings = _scan_tap(lines, cfg, budget)
    section = next((i for i, x in enumerate(lines) if _FAILING_HEADER.match(x)), None)
    # The spec tree ends where its errors section starts; with no section it is the whole log.
    tree, failed, suites = _scan_spec_tree(lines, section if section is not None else len(lines))
    entries.extend(tree)
    if section is not None:
        entries.extend(_scan_spec_section(lines, section + 1, failed, suites, cfg, budget))
    elif failed:
        entries.extend(_scan_spec_section(lines, len(lines), failed, suites, cfg, budget))
    entries.sort(key=lambda e: e.index)
    # A spec test that failed is both a tree result and (when described) a section failure: the
    # section's entry carries the error, the tree's carries the full name; keep the section one.
    stray_entries = _assign(groups, entries)
    runs: list[Run] = []
    failures: list[FailedTest] = []
    results: list[CaseResult] = []
    for group in groups:
        found, dropped = _failures_of(group.entries)
        found, _cut = dedupe_failures(found, cfg.parse.max_failures)
        failures.extend(found)
        results.extend(e.result for e in group.entries if e.result is not None)
        files = {f.file for f in found}
        runs.append(
            Run(
                summary=group.summary,
                tests=len(found) + dropped,
                files=None if None in files else len(files),
            )
        )
    stray_failures, _dropped = _failures_of(stray_entries)
    results.extend(e.result for e in stray_entries if e.result is not None)
    failures.extend(stray_failures)
    kept, cut = dedupe_failures(failures, cfg.parse.max_failures)
    unique, results_cut = dedupe_results(results, cfg.parse.max_results)
    if budget.exhausted or cut:
        warnings.append(
            f"node-test: stopped after {cfg.parse.max_failures} failure blocks; "
            "the rest are not read"
        )
    if results_cut:
        warnings.append(
            f"node-test: more than {cfg.parse.max_results} results; the rest are dropped"
        )
    granularity: Granularity = "test" if unique else "none"
    return PartialParse(
        results=tuple(unique),
        failures=tuple(kept),
        runs=tuple(runs),
        stray=len(stray_failures) if runs else 0,
        granularity=granularity,
        warnings=tuple(warnings),
        truncated=budget.exhausted or cut,
    )
