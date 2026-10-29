"""Triage evidence (P4.9): plain, testable code that reads one failure and says what it shows.

Four kinds of evidence, each a small function with no database and no model in it:

1. Stack versus diff (`overlap`): does the commit touch the failing test's file, or a file the
   stack trace passes through, or the lines around the frame? Frame paths are relative to the
   directory the tests ran from, so a path matches a changed file when one is a suffix of the other
   on a `/` boundary (a monorepo package prints `src/x.ts` for `packages/p/src/x.ts`).
2. Error type (`classify_errors`): which regex families the error text belongs to (below).
3. Test history (`history_stats`): flip rate, failure rate and mixed-outcome commits of the failing
   tests, from rows the loader already cut at the failure time.
4. Same-SHA evidence (`same_sha_stats`): other jobs on this commit that had already finished
   when this job failed.

Error families (`ERROR_FAMILIES`, case-insensitive; matched on the failing tests' messages and
the last `TAIL_CHARS` of the log, where crashes print):

| family | looks like | usually means |
|---|---|---|
| assertion | AssertionError, `expected X to equal Y`, `Expected:`/`Received:` | the code gave a wrong answer |
| snapshot | snapshot mismatch | output changed |
| code_error | TypeError, ReferenceError, `is not a function`, `Cannot read properties` | a code bug |
| compile | `error TS2345`, `Transform failed`, `Failed to compile` | a build-level mistake |
| module_missing | `Cannot find module`, `ERR_MODULE_NOT_FOUND` | a missing file or dependency |
| timeout | `Exceeded timeout of 5000 ms`, `timed out`, `ETIMEDOUT` | slow or stuck, often flaky |
| conn | `ECONNRESET`, `ECONNREFUSED`, `EPIPE`, `socket hang up` | a connection dropped |
| addrinuse | `EADDRINUSE` | a port was taken by another test or process |
| network | `ENOTFOUND`, `EAI_AGAIN`, `fetch failed`, `Bad Gateway`, rate limit | the network or a registry |
| oom | `heap out of memory`, `ENOMEM` | the runner ran out of memory |
| fs_resource | `ENOENT`, `EBUSY`, `EMFILE`, `ENOSPC` | file system trouble |
| killed | `SIGKILL`, `exit code 137`, `The operation was canceled`, runner shutdown | the process was stopped |
| unhandled | `UnhandledPromiseRejection` | an async error nobody caught |

The first block (assertion .. module_missing) leans genuine, the rest lean flaky or infrastructure;
this is a hint to the reader, the classifier learns the real weights from labelled failures.
"""
# ruff: noqa: E501

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from testing_expert.parsing import FailedTest

# DEFAULT: the last 4000 characters of the log are searched for crash messages (OOM, SIGKILL) that
# the runner prints after its report, outside any failing test.
TAIL_CHARS = 4_000
# DEFAULT: at most 20 failing tests are looked at per failure (history queries stay small).
MAX_TESTS = 20
# DEFAULT: a frame within 5 lines of a changed hunk counts as touching it (docs/experts/test.md 7).
LINE_SLACK = 5
# DEFAULT: at most 2000 characters of each failure message are searched.
MAX_MESSAGE_CHARS = 2_000

ERROR_FAMILIES: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "assertion": (
            r"AssertionError|assertion (?:failed|error)|expected.{0,80}\bto (?:be|equal|have|"
            r"include|contain|match|deep|not)|^\s*(?:Expected|Received)\b|toEqual|toBe\(|"
            r"toStrictEqual|deepEqual|strictEqual"
        ),
        "snapshot": r"snapshot",
        "code_error": (
            r"\b(?:TypeError|ReferenceError|RangeError)\b|is not a function|"
            r"cannot read propert(?:y|ies)|is not defined|is not iterable|undefined is not"
        ),
        "compile": r"error TS\d+|transform failed|failed to compile|\bSyntaxError\b",
        "module_missing": (
            r"cannot find module|ERR_MODULE_NOT_FOUND|module not found|failed to resolve"
        ),
        "timeout": (
            r"exceeded timeout of \d+|timeout of \d+ ?ms exceeded|timed out|timeout - async|"
            r"\bETIMEDOUT\b|\bESOCKETTIMEDOUT\b|TimeoutError"
        ),
        "conn": (
            r"\bECONNRESET\b|\bECONNREFUSED\b|\bEPIPE\b|\bECONNABORTED\b|socket hang up|"
            r"connection (?:reset|refused|closed|terminated)"
        ),
        "addrinuse": r"\bEADDRINUSE\b|address already in use",
        "network": (
            r"\bENOTFOUND\b|\bEAI_AGAIN\b|getaddrinfo|fetch failed|ERR_NETWORK|bad gateway|"
            r"service unavailable|gateway time-?out|rate limit"
        ),
        "oom": r"heap out of memory|javascript heap|\bENOMEM\b|out of memory|allocation failed",
        "fs_resource": r"\b(?:ENOENT|EBUSY|EMFILE|ENFILE|EACCES|EPERM|ENOSPC|ENOTEMPTY)\b",
        "killed": (
            r"\bSIGKILL\b|\bSIGTERM\b|exit code 137|\bkilled\b|the operation was canceled|"
            r"runner has received a shutdown|lost communication with the server"
        ),
        "unhandled": r"unhandled ?(?:promise)? ?rejection|unhandled error",
    }.items()
}
# Families that lean towards "real bug" and towards "environment" (used only for the reader's text).
GENUINE_LEANING = ("assertion", "snapshot", "code_error", "compile", "module_missing")
FLAKY_LEANING = ("timeout", "conn", "addrinuse", "network", "oom", "fs_resource", "killed")
FAMILY_TEXT = {
    "assertion": "an assertion mismatch (the code returned a value the test did not expect)",
    "snapshot": "a snapshot mismatch",
    "code_error": "a code error (TypeError, ReferenceError or a missing function)",
    "compile": "a compile or syntax error",
    "module_missing": "a missing module",
    "timeout": "a timeout",
    "conn": "a dropped or refused connection (ECONNRESET, ECONNREFUSED, socket hang up)",
    "addrinuse": "a port already in use (EADDRINUSE)",
    "network": "a network or registry error (ENOTFOUND, fetch failed, rate limit)",
    "oom": "an out-of-memory error",
    "fs_resource": "a file system error (ENOENT, EBUSY, EMFILE)",
    "killed": "the process being killed or the job being cancelled",
    "unhandled": "an unhandled promise rejection",
}


def failure_text(failures: Iterable[FailedTest]) -> str:
    """The error messages, assertion diffs and error types of the failing tests, joined."""
    parts: list[str] = []
    for f in list(failures)[:MAX_TESTS]:
        parts.append(f.error_type or "")
        parts.append(f.message[:MAX_MESSAGE_CHARS])
        parts.append((f.diff or "")[:MAX_MESSAGE_CHARS])
    return "\n".join(p for p in parts if p)


def classify_errors(text: str, tail: str = "") -> dict[str, bool]:
    """Which error families the failure text (and the log tail) matches."""
    haystack = text + "\n" + tail[-TAIL_CHARS:]
    return {name: bool(rx.search(haystack)) for name, rx in ERROR_FAMILIES.items()}


# --- stack versus diff -------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def hunk_ranges(patch: str | None) -> list[tuple[int, int]]:
    """New-file line ranges (first, last) of every hunk in a unified diff; empty without a patch."""
    if not patch:
        return []
    out = []
    for m in _HUNK.finditer(patch):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        out.append((start, start + max(count, 1) - 1))
    return out


def path_matches(frame_path: str, changed_path: str) -> bool:
    """True when the two paths are the same file (one may lack leading folders)."""
    a, b = frame_path.strip("/"), changed_path.strip("/")
    return bool(a) and (a == b or b.endswith("/" + a) or a.endswith("/" + b))


_TEST_PATH = re.compile(r"(^|/)(__tests__|tests?|spec|e2e)/|\.(test|spec)\.[cm]?[jt]sx?$")


def is_test_path(path: str) -> bool:
    """True for files that look like tests (`test/`, `__tests__/`, `*.test.ts`, `*.spec.js`)."""
    return bool(_TEST_PATH.search(path))


@dataclass(frozen=True)
class Overlap:
    """How the failure lines up with the commit's diff. Counts are None when not knowable."""

    commit_files_known: bool
    n_changed_files: int
    test_file_changed: bool | None = None  # a failing test's own file is in the diff
    test_files_total: int = 0
    stack_files_total: int = 0  # distinct project files in stack traces (besides the test file)
    stack_files_changed: int | None = None
    stack_source_changed: bool | None = None  # a changed file in the stack that is not a test
    frame_in_hunk: bool | None = None  # a frame within LINE_SLACK lines of a changed hunk
    changed_files: tuple[str, ...] = field(default=())
    first_test_file: str | None = None
    first_changed_stack_file: str | None = None
    first_hunk_frame: tuple[str, int] | None = None  # (path, line)

    @property
    def touches_changed_code(self) -> bool:
        """The stack trace (or the failing test itself) runs through code this commit changed."""
        return bool(self.test_file_changed or self.stack_files_changed or self.frame_in_hunk)


def overlap(failures: Sequence[FailedTest], changed: Mapping[str, str | None] | None) -> Overlap:
    """Compare failing test files and stack frames with the files of the commit.

    `changed` maps each changed path to its patch (None when GitHub gave no patch), or is None
    when the commit's files are unknown (then every overlap value stays unknown).
    """
    failures = list(failures)[:MAX_TESTS]
    test_files = sorted({f.file for f in failures if f.file})
    frames = [
        (fr.path, fr.line) for f in failures for fr in f.frames if fr.kind == "project" and fr.path
    ]
    stack_files = sorted({p for p, _ in frames if not any(path_matches(p, t) for t in test_files)})
    if changed is None:
        return Overlap(
            commit_files_known=False,
            n_changed_files=0,
            test_files_total=len(test_files),
            stack_files_total=len(stack_files),
            first_test_file=test_files[0] if test_files else None,
        )
    paths = tuple(changed)

    def find(p: str) -> str | None:
        return next((c for c in paths if path_matches(p, c)), None)

    test_changed = [t for t in test_files if find(t)]
    stack_changed = [p for p in stack_files if find(p)]
    first_hunk: tuple[str, int] | None = None
    for p, line in frames:
        c = find(p)
        if c is None or line is None:
            continue
        if any(lo - LINE_SLACK <= line <= hi + LINE_SLACK for lo, hi in hunk_ranges(changed[c])):
            first_hunk = (c, line)
            break
    any_patch = any(changed[p] for p in paths)
    return Overlap(
        commit_files_known=True,
        n_changed_files=len(paths),
        test_file_changed=bool(test_changed) if test_files else None,
        test_files_total=len(test_files),
        stack_files_total=len(stack_files),
        stack_files_changed=len(stack_changed) if frames else None,
        stack_source_changed=(any(not is_test_path(p) for p in stack_changed) if frames else None),
        frame_in_hunk=(first_hunk is not None) if (frames and any_patch) else None,
        changed_files=paths[:50],
        first_test_file=test_files[0] if test_files else None,
        first_changed_stack_file=(find(stack_changed[0]) if stack_changed else None),
        first_hunk_frame=first_hunk,
    )


# --- test history ------------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorTest:
    """What the database knew about one failing test before the failure (see loader)."""

    test_id: str
    n_runs: int  # earlier passed/failed results, finished before the failure
    n_failed: int
    n_flips: int  # pass<->fail changes between consecutive earlier results
    n_mixed_shas: int  # earlier commits where the test both failed and passed


@dataclass(frozen=True)
class HistoryStats:
    """History of the failing tests together. Values are None when no test has any history."""

    n_tests: int
    known: bool
    frac_new: float | None = None  # share of tests never seen before
    frac_never_failed: float | None = None  # share seen before, but never failed before
    max_fail_rate: float | None = None
    max_flip_rate: float | None = None
    max_mixed_shas: int | None = None
    max_prior_failures: int | None = None
    worst_test_id: str | None = None  # test with the most mixed commits, for the reason text


def history_stats(n_failing: int, records: Sequence[PriorTest]) -> HistoryStats:
    """Aggregate per-test history over the failing tests (one record per test with history)."""
    recs = [r for r in records if r.n_runs > 0]
    if n_failing == 0 or not recs:
        return HistoryStats(n_tests=n_failing, known=False, frac_new=1.0 if n_failing else None)
    worst = max(recs, key=lambda r: (r.n_mixed_shas, r.n_flips))
    return HistoryStats(
        n_tests=n_failing,
        known=True,
        frac_new=(n_failing - len(recs)) / n_failing,
        frac_never_failed=sum(1 for r in recs if r.n_failed == 0) / n_failing,
        max_fail_rate=max(r.n_failed / r.n_runs for r in recs),
        max_flip_rate=max(r.n_flips / (r.n_runs - 1) if r.n_runs > 1 else 0.0 for r in recs),
        max_mixed_shas=max(r.n_mixed_shas for r in recs),
        max_prior_failures=max(r.n_failed for r in recs),
        worst_test_id=worst.test_id,
    )


# --- same-SHA evidence -------------------------------------------------------------------------


@dataclass(frozen=True)
class SiblingJob:
    """Another job on the same commit that had finished when this job failed."""

    job_id: int
    name: str
    workflow_path: str
    attempt: int
    conclusion: str  # success | failure
    completed_at_ts: float


_MATRIX_SUFFIX = re.compile(r"\s*\(.*$")


def family_key(name: str | None) -> str:
    """Job name without its `(matrix)` part: `Tests (node 22, ubuntu)` -> `Tests`."""
    return _MATRIX_SUFFIX.sub("", name or "").strip().lower()


@dataclass(frozen=True)
class SameSha:
    """What other jobs on this commit showed at the time of the failure (nothing from later)."""

    attempt: int
    same_job_pass: int
    same_job_fail: int
    family_pass: int  # other legs of the same matrix job
    family_fail: int
    other_pass: int  # unrelated jobs on this commit
    other_fail: int
    first_pass_job: str | None = None
    first_pass_attempt: int | None = None


def same_sha_stats(
    attempt: int,
    job_name: str | None,
    workflow_path: str,
    siblings: Sequence[SiblingJob],
    cutoff_ts: float,
) -> SameSha:
    """Count earlier-finished jobs on the same commit; anything after `cutoff_ts` is ignored."""
    counts = {"sp": 0, "sf": 0, "fp": 0, "ff": 0, "op": 0, "of": 0}
    first_pass: SiblingJob | None = None
    fam = family_key(job_name)
    for s in siblings:
        if s.completed_at_ts > cutoff_ts or s.conclusion not in ("success", "failure"):
            continue  # not finished when this job failed (or not a pass/fail result)
        ok = s.conclusion == "success"
        if s.workflow_path == workflow_path and (s.name or "") == (job_name or ""):
            counts["sp" if ok else "sf"] += 1
        elif s.workflow_path == workflow_path and family_key(s.name) == fam:
            counts["fp" if ok else "ff"] += 1
        else:
            counts["op" if ok else "of"] += 1
        if ok and (first_pass is None or s.completed_at_ts < first_pass.completed_at_ts):
            first_pass = s
    return SameSha(
        attempt=attempt,
        same_job_pass=counts["sp"],
        same_job_fail=counts["sf"],
        family_pass=counts["fp"],
        family_fail=counts["ff"],
        other_pass=counts["op"],
        other_fail=counts["of"],
        first_pass_job=first_pass.name if first_pass else None,
        first_pass_attempt=first_pass.attempt if first_pass else None,
    )


def is_nan(x: float | None) -> bool:
    """True for None and NaN."""
    return x is None or (isinstance(x, float) and math.isnan(x))
