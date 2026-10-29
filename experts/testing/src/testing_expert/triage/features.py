"""From evidence to the triage feature vector and to human-readable reasons (P4.9).

`TriageEvidence` bundles the four kinds of evidence plus the pre-run flaky risk. `feature_row`
turns it into the numbers the classifier reads (NaN = unknown, never zero), and `reasons` turns the
same evidence into sentences for the developer. Both read the same object, so what the model saw
and what the developer is told cannot drift apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from testing_expert.extract import make_test_id
from testing_expert.parsing import FailedTest, ParsedLog, parse_log

from .evidence import (
    ERROR_FAMILIES,
    FAMILY_TEXT,
    FLAKY_LEANING,
    GENUINE_LEANING,
    MAX_TESTS,
    HistoryStats,
    Overlap,
    SameSha,
    classify_errors,
    failure_text,
)

FRAMEWORKS = ("jest", "mocha", "vitest")
Leans = Literal["flaky", "genuine", "neutral"]
ReasonSource = Literal["log", "stack_trace", "diff", "history", "same_sha", "pre_run"]

FEATURE_NAMES: tuple[str, ...] = (
    # stack versus diff
    "ov_files_known",
    "ov_n_changed_files",
    "ov_test_file_changed",
    "ov_stack_files",
    "ov_stack_files_changed",
    "ov_stack_source_changed",
    "ov_frame_in_hunk",
    # error types
    *(f"er_{name}" for name in ERROR_FAMILIES),
    "er_n_families",
    "er_leans_flaky",
    "er_leans_genuine",
    "lg_n_failed_tests",
    "lg_n_failed_files",
    "lg_parsed_ok",
    *(f"lg_fw_{fw}" for fw in FRAMEWORKS),
    # failing tests' own history
    "th_known",
    "th_frac_new",
    "th_frac_never_failed",
    "th_max_fail_rate",
    "th_max_flip_rate",
    "th_max_mixed_shas",
    "th_max_prior_failures",
    # same-SHA evidence available at failure time
    "ss_attempt",
    "ss_same_job_pass",
    "ss_same_job_fail",
    "ss_family_pass",
    "ss_family_fail",
    "ss_other_pass",
    "ss_other_fail",
    # pre-run model
    "pr_flaky_risk",
)
# Feature groups for the ablation study (which kind of evidence carries the verdict).
GROUPS: dict[str, tuple[str, ...]] = {
    "overlap": tuple(n for n in FEATURE_NAMES if n.startswith("ov_")),
    "errors": tuple(n for n in FEATURE_NAMES if n.startswith(("er_", "lg_"))),
    "history": tuple(n for n in FEATURE_NAMES if n.startswith("th_")),
    "same_sha": tuple(n for n in FEATURE_NAMES if n.startswith("ss_")),
    "pre_run": ("pr_flaky_risk",),
}
NAN = float("nan")


@dataclass(frozen=True)
class LogEvidence:
    """What the (sanitised) log says: failing tests, their stack frames and error families."""

    framework: str
    parsed_ok: bool
    failures: tuple[FailedTest, ...]
    test_ids: tuple[str, ...]  # same ids as testing.test_results
    errors: dict[str, bool]
    n_failed_files: int

    @property
    def is_test_failure(self) -> bool:
        """True when a Jest, Mocha or Vitest failure was found in the log."""
        return bool(self.failures) and self.framework in FRAMEWORKS


def read_log(text: str, repo: str) -> LogEvidence:
    """Parse a log (or the sanitised tail the router sends) into `LogEvidence`."""
    parsed: ParsedLog = parse_log(text)
    failures = tuple(f for f in parsed.failures if f.kind != "unhandled")[:MAX_TESTS]
    ids = tuple(
        dict.fromkeys(
            make_test_id(repo, f.file, f.full_name if f.kind == "test" else "")
            for f in failures
            if f.file or f.kind == "test"
        )
    )
    return LogEvidence(
        framework=parsed.framework,
        parsed_ok=parsed.parsed_ok,
        failures=failures,
        test_ids=ids,
        errors=classify_errors(failure_text(parsed.failures), text),
        n_failed_files=len({f.file for f in failures if f.file}),
    )


@dataclass(frozen=True)
class TriageEvidence:
    """Everything triage knows about one failure."""

    log: LogEvidence
    overlap: Overlap
    history: HistoryStats
    same_sha: SameSha | None  # None when the run is not in the database
    prerun_flaky: float | None
    extra: dict[str, str] = field(default_factory=dict)


def _num(value: float | bool | int | None) -> float:
    return NAN if value is None else float(value)


def feature_row(ev: TriageEvidence) -> dict[str, float]:
    """The classifier's input for one failure; NaN marks "not known"."""
    o, h, s, lg = ev.overlap, ev.history, ev.same_sha, ev.log
    row: dict[str, float] = {
        "ov_files_known": float(o.commit_files_known),
        "ov_n_changed_files": float(o.n_changed_files) if o.commit_files_known else NAN,
        "ov_test_file_changed": _num(o.test_file_changed),
        "ov_stack_files": float(o.stack_files_total),
        "ov_stack_files_changed": _num(o.stack_files_changed),
        "ov_stack_source_changed": _num(o.stack_source_changed),
        "ov_frame_in_hunk": _num(o.frame_in_hunk),
    }
    for name in ERROR_FAMILIES:
        row[f"er_{name}"] = float(lg.errors.get(name, False))
    row["er_n_families"] = float(sum(lg.errors.values()))
    row["er_leans_flaky"] = float(any(lg.errors.get(n) for n in FLAKY_LEANING))
    row["er_leans_genuine"] = float(any(lg.errors.get(n) for n in GENUINE_LEANING))
    row["lg_n_failed_tests"] = float(len(lg.failures))
    row["lg_n_failed_files"] = float(lg.n_failed_files)
    row["lg_parsed_ok"] = float(lg.parsed_ok)
    for fw in FRAMEWORKS:
        row[f"lg_fw_{fw}"] = float(lg.framework == fw)
    row.update(
        th_known=float(h.known),
        th_frac_new=_num(h.frac_new),
        th_frac_never_failed=_num(h.frac_never_failed),
        th_max_fail_rate=_num(h.max_fail_rate),
        th_max_flip_rate=_num(h.max_flip_rate),
        th_max_mixed_shas=_num(h.max_mixed_shas),
        th_max_prior_failures=_num(h.max_prior_failures),
    )
    row.update(
        ss_attempt=_num(s.attempt) if s else NAN,
        ss_same_job_pass=_num(s.same_job_pass) if s else NAN,
        ss_same_job_fail=_num(s.same_job_fail) if s else NAN,
        ss_family_pass=_num(s.family_pass) if s else NAN,
        ss_family_fail=_num(s.family_fail) if s else NAN,
        ss_other_pass=_num(s.other_pass) if s else NAN,
        ss_other_fail=_num(s.other_fail) if s else NAN,
        pr_flaky_risk=_num(ev.prerun_flaky),
    )
    return {name: row[name] for name in FEATURE_NAMES}


@dataclass(frozen=True)
class Reason:
    """One piece of evidence as a sentence, and which verdict it points to."""

    source: ReasonSource
    text: str
    leans: Leans = "neutral"
    path: str | None = None
    line: int | None = None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def reasons(ev: TriageEvidence) -> list[Reason]:
    """The evidence as plain sentences (every item the classifier saw that has something to say)."""
    out: list[Reason] = []
    o, h, s, lg = ev.overlap, ev.history, ev.same_sha, ev.log

    # stack versus diff
    if not o.commit_files_known:
        out.append(
            Reason(
                "diff",
                "The files this commit changed are not known, so the stack trace could not be "
                "compared with the diff.",
            )
        )
    else:
        if o.test_file_changed:
            out.append(
                Reason(
                    "diff",
                    f"This commit changed the failing test file {o.first_test_file}.",
                    "genuine",
                    path=o.first_test_file,
                )
            )
        if o.first_hunk_frame is not None:
            p, line = o.first_hunk_frame
            out.append(
                Reason(
                    "stack_trace",
                    f"The stack trace passes through {p} line {line}, within 5 lines of code "
                    "this commit changed.",
                    "genuine",
                    path=p,
                    line=line,
                )
            )
        elif o.first_changed_stack_file:
            out.append(
                Reason(
                    "stack_trace",
                    f"The stack trace passes through {o.first_changed_stack_file}, a file this "
                    "commit changed.",
                    "genuine",
                    path=o.first_changed_stack_file,
                )
            )
        if (
            not o.touches_changed_code
            and (o.test_files_total or o.stack_files_total)
            and o.test_file_changed is not None
        ):
            out.append(
                Reason(
                    "diff",
                    "Neither the failing test file nor any file in its stack trace was changed "
                    f"by this commit ({_plural(o.n_changed_files, 'file')} changed).",
                    "flaky",
                )
            )

    # error type
    hits = [n for n, v in lg.errors.items() if v]
    for name in hits:
        lean: Leans = (
            "genuine"
            if name in GENUINE_LEANING
            else "flaky"
            if name in FLAKY_LEANING
            else "neutral"
        )
        out.append(Reason("log", f"The log shows {FAMILY_TEXT[name]}.", lean))
    if not hits:
        out.append(Reason("log", "The log shows no known error pattern."))

    # test history
    if h.known:
        if h.max_mixed_shas:
            out.append(
                Reason(
                    "history",
                    f"A failing test both passed and failed on the same commit on "
                    f"{_plural(h.max_mixed_shas, 'earlier commit')} (a flaky pattern).",
                    "flaky",
                )
            )
        if h.max_flip_rate is not None and h.max_flip_rate > 0:
            out.append(
                Reason(
                    "history",
                    f"A failing test changed between pass and fail in {h.max_flip_rate:.0%} of "
                    "its earlier runs.",
                    "flaky" if h.max_flip_rate >= 0.05 else "neutral",
                )
            )
        if h.frac_never_failed == 1.0:
            out.append(
                Reason("history", "None of the failing tests had ever failed before.", "genuine")
            )
    else:
        out.append(
            Reason(
                "history",
                "The failing tests have no earlier results on record (new tests or no history).",
                "genuine" if h.n_tests else "neutral",
            )
        )

    # same-SHA evidence
    if s is not None:
        if s.same_job_pass:
            who = f" (attempt {s.first_pass_attempt})" if s.first_pass_attempt else ""
            out.append(
                Reason(
                    "same_sha",
                    f"The same job already passed on this commit{who} before it failed "
                    "(same code, different result).",
                    "flaky",
                )
            )
        if s.family_pass and not s.family_fail:
            out.append(
                Reason(
                    "same_sha",
                    f"{_plural(s.family_pass, 'other matrix leg')} of this job passed on this "
                    "commit before this one failed.",
                    "flaky",
                )
            )
        if s.same_job_fail:
            out.append(
                Reason(
                    "same_sha",
                    f"This job already failed {_plural(s.same_job_fail, 'time')} on this commit "
                    "before this attempt (it keeps failing).",
                    "genuine",
                )
            )
        if s.family_fail and not s.family_pass:
            out.append(
                Reason(
                    "same_sha",
                    f"{_plural(s.family_fail, 'other matrix leg')} of this job also failed on "
                    "this commit.",
                    "genuine",
                )
            )
        if s.attempt > 1:
            out.append(Reason("same_sha", f"This is attempt {s.attempt} of the run (a re-run)."))
        if not (s.same_job_pass or s.same_job_fail or s.family_pass or s.family_fail):
            out.append(
                Reason(
                    "same_sha",
                    "No run of this job (or of its matrix legs) had finished on this commit "
                    "when it failed.",
                )
            )
    else:
        out.append(
            Reason(
                "same_sha",
                "Other jobs on this commit could not be looked up, so there is no same-commit "
                "evidence.",
            )
        )

    # pre-run model
    if ev.prerun_flaky is not None and not math.isnan(ev.prerun_flaky):
        out.append(
            Reason(
                "pre_run",
                f"Before the run, the model rated the chance of a flaky failure at "
                f"{ev.prerun_flaky:.0%}.",
                "flaky" if ev.prerun_flaky >= 0.2 else "neutral",
            )
        )
    return out
