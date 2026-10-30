"""Plain data the log parsers return. Frozen dataclasses: parsers build them, nobody edits them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# The first three and "other" match BehaviourDetails.framework in shared/contracts/diagnosis.py.
# "node-test" (Node's built-in runner) is not in that contract: `TriageResult.framework` maps it
# to "other" where the contract is filled in.
Framework = Literal["jest", "mocha", "vitest", "node-test", "other"]
# Where a stack frame points: the repo's own code, a third-party package, Node itself, or unknown.
FrameKind = Literal["project", "dependency", "node_internal", "unknown"]
Outcome = Literal["passed", "failed", "skipped", "todo"]
# What the reporter printed: one line per test, one per file, or nothing usable.
Granularity = Literal["test", "file", "none"]
# Where a failure's file name came from: the `FAIL file` header, its first project stack frame,
# or a summary line.
FileSource = Literal["header", "stack", "summary"]
# What failed: one test (or hook), a whole file that could not run ("Test suite failed to run"), or
# an error the runner caught outside any test (vitest "Unhandled Errors").
FailureKind = Literal["test", "suite", "unhandled"]

# DEFAULT: the plan calls these TestFailure and TestResult, but CONVENTIONS forbids `Test*` classes
# under src/ (pytest would try to collect them), so they are FailedTest and CaseResult.


@dataclass(frozen=True, slots=True)
class StackFrame:
    """One line of a stack trace.

    `path` has the runner workspace prefix removed, so it is relative to the directory the tests
    ran from: the repo root when the runner started there, a package folder (`src/x.ts` for
    `packages/p/src/x.ts`) in a monorepo run from the package. It is None when the frame has no
    usable file (Node internals, `<anonymous>`, temp files, browser URLs, `../` paths). Whether
    the file exists in the commit is NOT checked here.
    """

    function: str | None
    raw: str
    path: str | None
    line: int | None
    col: int | None
    kind: FrameKind


@dataclass(frozen=True, slots=True)
class FailedTest:
    """One failing test with its error."""

    full_name: str
    name_parts: tuple[str, ...]
    file: str | None
    file_source: FileSource | None
    error_type: str | None
    message: str
    diff: str | None = None
    frames: tuple[StackFrame, ...] = ()
    hook: str | None = None
    retries: int = 0
    raw: str = ""
    kind: FailureKind = "test"


@dataclass(frozen=True, slots=True)
class CaseResult:
    """The outcome of one test (or one file, when the reporter only prints files)."""

    full_name: str
    file: str | None
    outcome: Outcome
    duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class Summary:
    """The totals the runner printed at the end.

    A field is None when no line gave it. Jest and vitest print only the non-zero counts, so a
    count they leave out comes back as 0.
    """

    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    todo: int | None = None
    total: int | None = None
    files_failed: int | None = None
    duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ParsedLog:
    """What `parse_log` returns for one job log. It is built even when parsing went wrong."""

    framework: Framework
    results: tuple[CaseResult, ...] = ()
    failures: tuple[FailedTest, ...] = ()
    summary: Summary | None = None
    granularity: Granularity = "none"
    parsed_ok: bool = True
    confidence: float = 0.0
    warnings: tuple[str, ...] = field(default_factory=tuple)
    truncated: bool = False
    # How many runner summaries the log held (retries and per-package runs print several); `summary`
    # adds them up.
    summary_count: int = 0

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be between 0 and 1, got {self.confidence}")
