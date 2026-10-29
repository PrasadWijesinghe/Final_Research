"""Decide whether one failed job attempt is a build or code-quality failure.

The rules only ever claim the `build` category. A job they do not claim is left for the other
members' rules (dependency, test, infra). Each decision names the rule that fired, so a label can
always be traced back (docs/experts/build.md section 3 has the table).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from shared.contracts import FailureCategory, FailureLabel, RepoRef

from ..parsing.signals import JobSignals, StepSignals
from ..parsing.tools import Finding

# DEFAULT: bump when a rule changes; datasets and labels record it.
LABEL_RULES_VERSION = "1"

Confidence = Literal["high", "medium", "low"]
FAILED = frozenset({"failure"})
NOT_CLAIMED = frozenset({"timed_out", "startup_failure", "cancelled", "skipped"})

# Step names (from the API) or commands (from the log) that build, type-check, lint or format.
_BUILD_STEP = re.compile(
    r"(?i)(?<![\w-])(?:build|compile|tsc|vue-tsc|svelte-check|type-?check|check-types|types|"
    r"lint|eslint|oxlint|biome|prettier|format(?:ting|:check)?|fmt|bundle|webpack|vite build|"
    r"rollup|esbuild|tsup|next build|nuxi build|astro (?:build|check))(?![\w-])"
)
# Steps whose failure is about setup, dependencies or the runner, never about the code.
_SETUP_STEP = re.compile(
    r"(?i)^(?:set up job|complete job|post |run actions/(?:checkout|setup-node|cache)|"
    r"checkout|set ?up node|setup node|use node|install(?: dependencies| deps)?$|cache|"
    r"run (?:npm|pnpm|yarn|bun)(?: -\S+)* (?:ci|install|i)\b|"
    r"run (?:pnpm/action-setup|corepack enable|npm i(?:nstall)? -g))"
)
_TEST_STEP = re.compile(r"(?i)(?<![\w-])(?:test|tests|jest|mocha|vitest|playwright|e2e|cypress)\b")
_SUB_FROM_STEP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)tsc|type-?check|check-types|types\b|vue-tsc|svelte-check"), "tsc"),
    (re.compile(r"(?i)prettier|format|fmt"), "format"),
    (re.compile(r"(?i)lint|eslint|oxlint|biome"), "eslint"),
    (re.compile(r"(?i)webpack|vite|rollup|esbuild|tsup|bundle"), "bundler"),
]
_DOWNGRADE: dict[Confidence, Confidence] = {"high": "medium", "medium": "low", "low": "low"}


@dataclass(frozen=True)
class StepFacts:
    """One step as reported by the GitHub API (raw.steps)."""

    number: int
    name: str | None
    conclusion: str | None


@dataclass(frozen=True)
class JobFacts:
    """What the labeller needs about one job attempt, from raw.jobs, raw.steps and re-runs."""

    repo: str  # owner/name
    run_id: int
    attempt: int
    job_id: int
    conclusion: str | None
    name: str | None = None
    job_key: str | None = None
    steps: Sequence[StepFacts] = ()
    passed_on_rerun: bool = False  # the same job passed in a later attempt on the same SHA


@dataclass(frozen=True)
class LabelDecision:
    """The labeller's verdict for one job. `claimed=False` leaves the job to other members."""

    claimed: bool
    rule_name: str
    sub_label: str | None = None  # tsc | eslint | format | bundler | build_other
    confidence: Confidence = "high"
    tool: Literal["tsc", "eslint", "bundler", "other"] = "other"
    notes: str | None = None
    findings: tuple[Finding, ...] = field(default=(), compare=False)


def is_build_command(text: str | None) -> bool:
    """True when a step name or command looks like build, type-check, lint or format."""
    return bool(text) and bool(_BUILD_STEP.search(text or "")) and not _is_setup(text)


def _is_setup(text: str | None) -> bool:
    return bool(text) and bool(_SETUP_STEP.search((text or "").strip()))


def _is_test(text: str | None) -> bool:
    return bool(text) and bool(_TEST_STEP.search(text or "")) and not _BUILD_STEP.search(text or "")


def _first_failed_step(steps: Sequence[StepFacts]) -> StepFacts | None:
    failed = [s for s in steps if s.conclusion == "failure"]
    return min(failed, key=lambda s: s.number) if failed else None


def _sub_from_name(text: str) -> str:
    for pattern, sub in _SUB_FROM_STEP:
        if pattern.search(text):
            return sub
    return "build_other"


def _tool_literal(sub: str) -> Literal["tsc", "eslint", "bundler", "other"]:
    if sub == "tsc":
        return "tsc"
    if sub == "eslint":
        return "eslint"
    if sub == "bundler":
        return "bundler"
    return "other"


def _primary(findings: Sequence[Finding]) -> Finding | None:
    """The tool that explains the failure: the first one that reported a failure."""
    failing = [f for f in findings if f.failed]
    return failing[0] if failing else None


def _claim(
    rule: str, sub: str, confidence: Confidence, findings: Sequence[Finding], notes: str | None
) -> LabelDecision:
    return LabelDecision(
        claimed=True,
        rule_name=rule,
        sub_label=sub,
        confidence=confidence,
        tool=_tool_literal(sub),
        notes=notes,
        findings=tuple(findings),
    )


def _from_log(job: JobFacts, failed_api: StepFacts | None, step: StepSignals) -> LabelDecision:
    """Rules that use the failed step's log section."""
    names = " ".join(x for x in (step.title, failed_api.name if failed_api else None) if x)
    primary = _primary(step.findings)
    blocking = step.hints & {"resource", "runner", "dependency"}
    if primary is None:
        if blocking:
            return LabelDecision(False, f"not_build_{sorted(blocking)[0]}")
        if "test" in step.hints or _is_test(names):
            return LabelDecision(False, "not_build_test_failure")
        if is_build_command(names):
            return _claim("build_step_no_signature", "build_other", "medium", (), None)
        return LabelDecision(False, "not_build_no_signature")
    tools = ",".join(sorted({f.tool for f in step.findings}))
    note = f"tools in step: {tools}" if "," in tools else None
    if blocking:
        # A real tool error next to an out-of-memory or dependency error: flag for review.
        return _claim(
            "tool_error_with_other_cause",
            primary.sub_label,
            "low",
            step.findings,
            f"also saw: {', '.join(sorted(blocking))}",
        )
    if "test" in step.hints:
        return _claim("tool_error_in_test_step", primary.sub_label, "low", step.findings, note)
    if is_build_command(names):
        return _claim("build_step_tool_error", primary.sub_label, "high", step.findings, note)
    return _claim("tool_error_other_step", primary.sub_label, "medium", step.findings, note)


def _from_whole_log(
    job: JobFacts, failed_api: StepFacts | None, signals: JobSignals
) -> LabelDecision:
    """No exit-code marker (log cut short): read the whole log as one block, with less trust."""
    merged = StepSignals(
        index=0,
        title="",
        exit_code=1,
        findings=[f for s in signals.steps for f in s.findings],
        hints=set().union(*(s.hints for s in signals.steps)) if signals.steps else set(),
    )
    decision = _from_log(job, failed_api, merged)
    if not decision.claimed:
        return decision
    return _claim(
        decision.rule_name + "_whole_log",
        decision.sub_label or "build_other",
        _DOWNGRADE[decision.confidence],
        decision.findings,
        decision.notes,
    )


def _from_step_name(failed_api: StepFacts | None) -> LabelDecision:
    """No log at all (expired or never fetched): only the API step name is left."""
    name = failed_api.name if failed_api else None
    if name and is_build_command(name) and not _is_test(name):
        return _claim("build_step_name_only", _sub_from_name(name), "low", (), None)
    return LabelDecision(False, "not_build_no_log")


def decide(job: JobFacts, signals: JobSignals | None) -> LabelDecision:
    """Apply the build labelling rules to one job attempt (first matching rule wins)."""
    if job.conclusion not in FAILED:
        reason = "not_failed" if job.conclusion not in NOT_CLAIMED else f"job_{job.conclusion}"
        return LabelDecision(False, reason)
    failed_api = _first_failed_step(job.steps)
    if failed_api is not None and _is_setup(failed_api.name):
        return LabelDecision(False, "not_build_setup_step")

    if signals is not None and signals.failed_steps:
        decision = _from_log(job, failed_api, signals.failed_steps[0])
    elif signals is not None:
        decision = _from_whole_log(job, failed_api, signals)
    else:
        decision = _from_step_name(failed_api)

    if decision.claimed and job.passed_on_rerun:
        notes = "; ".join(x for x in (decision.notes, "passed on a re-run of the same SHA") if x)
        decision = _claim(
            decision.rule_name,
            decision.sub_label or "build_other",
            _DOWNGRADE[decision.confidence],
            decision.findings,
            notes,
        )
    return decision


def to_failure_label(job: JobFacts, decision: LabelDecision) -> FailureLabel:
    """Turn a claimed decision into the shared FailureLabel contract."""
    if not decision.claimed:
        raise ValueError("only claimed decisions become FailureLabels")
    owner, name = job.repo.split("/", 1)
    return FailureLabel(
        repo=RepoRef(owner=owner, name=name),
        run_id=job.run_id,
        run_attempt=job.attempt,
        job_id=job.job_id,
        category=FailureCategory.BUILD,
        sub_label=decision.sub_label,
        labelled_by="rule",
        rule_name=f"build.{decision.rule_name}.v{LABEL_RULES_VERSION}",
        confidence=decision.confidence,
        notes=decision.notes,
    )
