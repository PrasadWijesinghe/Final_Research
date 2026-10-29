"""Diagnosis of a failed job (PLAN B11; docs/experts/build.md section 8).

1. Read the job's full sanitised log from shared storage (`DATA_DIR/<log_ref>`), or the excerpt.
2. Run the same parsers and labelling rules as the dataset. If the rules say this is not a build
   failure, answer `misrouted` with the category the evidence points to.
3. Otherwise fill `BuildDetails` (tool and error locations) and cite the error lines as evidence.
4. Retrieve similar past failures and their fixes from the knowledge base (`build.kb_cases`).
5. Write the root cause and the remediation from the parsed errors and the retrieved fixes. An LLM
   (through the team's gateway) may rewrite them when BUILD_LLM=on; it may cite only retrieved
   cases, and any problem falls back to the written text.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path

from shared.contracts import (
    BuildDetails,
    Diagnosis,
    EvidenceLine,
    FailureCategory,
    Remediation,
    RetrievedCase,
    RoutedFailure,
)
from shared.contracts.diagnosis import BuildLocation

from .config import EXPERT
from .labels import JobFacts, LabelDecision, StepFacts, decide
from .parsing import JobSignals, parse_job_log
from .parsing.clean import clean_line, failed_sections, split_sections
from .parsing.tools import Finding
from .rag.kb import CaseSearch
from .rag.llm import refine_with_llm

logger = logging.getLogger(__name__)

_MAX_EVIDENCE_CHARS = 1_000  # EvidenceLine.text limit in the contract
# DEFAULT: how much is shown and cited.
MAX_EVIDENCE = 5
MAX_LOCATIONS = 20
CASES_K = 3
# DEFAULT: confidence by the labelling rule's confidence.
CONFIDENCE = {"high": 0.9, "medium": 0.7, "low": 0.5}
# Where a declined job most likely belongs, by the rule that declined it.
MISROUTED = {
    "not_build_dependency": FailureCategory.DEPENDENCY,
    "not_build_test_failure": FailureCategory.TEST,
    "not_build_resource": FailureCategory.INFRA,
    "not_build_runner": FailureCategory.INFRA,
}
ERROR_NOUN = {
    "tsc": "TypeScript",
    "eslint": "ESLint",
    "format": "formatting",
    "bundler": "bundler",
    "build_other": "build",
}
TOOL_NAMES = {
    "tsc": "TypeScript compilation",
    "eslint": "ESLint",
    "format": "the formatting check",
    "bundler": "the bundler",
    "build_other": "the build step",
}
STEPS = {
    "tsc": [
        "Run the repo's type-check script (or `tsc --noEmit`) locally to see the same errors.",
        "Fix the types at the listed locations; check imports and renamed or removed exports.",
    ],
    "eslint": [
        "Run the repo's lint script locally (for example `npm run lint`).",
        "Fix the listed rule violations, or use the linter's --fix option where it is safe.",
    ],
    "format": [
        "Run the formatter in write mode (for example `npx prettier --write` on the listed files).",
        "Commit the reformatted files.",
    ],
    "bundler": [
        "Run the production build locally (for example `npm run build`).",
        "Check the failing import or plugin: missing files, wrong paths, or a changed config.",
    ],
    "build_other": [
        "Open the failed step's log and find the first error line.",
        "Run the same command locally to reproduce it.",
    ],
}


# DEFAULT: shared storage is DATA_DIR, else <checkout>/data (as for the collector and datasets).
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[4] / "data"


def _read_log(failure: RoutedFailure) -> str:
    """The full sanitised log when shared storage has it, else the excerpt (the excerpt is only
    the tail, so the failed step's start can be missing and the rules may decide differently)."""
    data_dir = os.environ.get("DATA_DIR") or str(DEFAULT_DATA_DIR)
    if failure.log_ref:
        path = Path(data_dir) / failure.log_ref
        try:
            raw = path.read_bytes()
            return (gzip.decompress(raw) if path.suffix == ".gz" else raw).decode(
                "utf-8", "replace"
            )
        except OSError:
            pass
    return failure.log_excerpt


# A line that reports a problem; GitHub's own exit-code line says nothing about the cause.
_PROBLEM = re.compile(r"error|ERR!|fail|✗|×|cannot|not found|denied|killed|timed? ?out", re.I)
# Lines that only say "the command failed" (the package manager's or GitHub's own), and the
# script echo lines npm/pnpm print first: never evidence of the cause.
_GITHUB_FOOTER = re.compile(
    r"^##\[error\]Process completed with exit code|^Post job cleanup|ELIFECYCLE|"
    r"Command failed with exit code|^npm (?:ERR!|error) (?:code|errno|path|command|"
    r"A complete log|Lifecycle script)|^error Command failed|exited with (?:exit )?code|^> "
)
# DEFAULT: a short step output (up to this many lines) is cited from its start, a long one from
# its end, when no line names a problem.
SHORT_OUTPUT = 12
FALLBACK_LINES = 3


def _fallback_evidence(failure: RoutedFailure, text: str) -> list[EvidenceLine]:
    """Without parsed errors: the first line of the failed step that names a problem (usually
    the cause; the last lines are summaries); else the step's own message (a short output from
    its start, a long one from its end); else the step name."""
    failed = failed_sections(split_sections(text)) if text else []
    if failed:
        lines = [ln.strip() for ln in failed[0].lines]
    else:
        lines = [clean_line(ln).strip() for ln in (text or failure.log_excerpt).splitlines()]
    lines = [ln for ln in lines if ln and not _GITHUB_FOOTER.search(ln)]
    problems = [ln for ln in lines if _PROBLEM.search(ln)]
    if problems:
        chosen = [problems[0]]
    elif lines:
        n = FALLBACK_LINES
        chosen = lines[:n] if len(lines) <= SHORT_OUTPUT else lines[-n:]
    else:
        chosen = [f"failed step: {failure.failed_steps[0].name}"]
    return [EvidenceLine(source="log", text=c[:_MAX_EVIDENCE_CHARS]) for c in chosen]


def _facts(failure: RoutedFailure) -> JobFacts:
    return JobFacts(
        repo=failure.repo.full_name,
        run_id=failure.run_id,
        attempt=failure.run_attempt,
        job_id=failure.job_id,
        conclusion="failure",
        name=failure.job_name,
        job_key=failure.job_key,
        steps=tuple(StepFacts(s.number, s.name, "failure") for s in failure.failed_steps),
    )


_INSTALL = re.compile(r"\b(?:install|ci|i|add)\b|\byarn\s*$", re.I)


def _misrouted_category(
    decision: LabelDecision, failure: RoutedFailure, signals: JobSignals | None
) -> FailureCategory:
    if decision.rule_name in MISROUTED:
        return MISROUTED[decision.rule_name]
    if decision.rule_name == "not_build_setup_step":
        # A failed setup step: installing packages points at dependencies, anything else
        # (checkout, setup-node, caches) at the infrastructure.
        hints = set().union(*(s.hints for s in signals.steps)) if signals else set()
        step = failure.failed_steps[0].name
        if "dependency" in hints or _INSTALL.search(step):
            return FailureCategory.DEPENDENCY
        return FailureCategory.INFRA
    return FailureCategory.UNKNOWN


def _locations(findings: tuple[Finding, ...]) -> list[BuildLocation]:
    out: list[BuildLocation] = []
    for f in findings:
        for loc in f.locations:
            if loc.path and len(out) < MAX_LOCATIONS:
                out.append(BuildLocation(path=loc.path, line=loc.line, code=loc.code))
    return out


def _evidence(findings: tuple[Finding, ...]) -> list[EvidenceLine]:
    out: list[EvidenceLine] = []
    for f in findings:
        for loc in f.locations:
            if len(out) == MAX_EVIDENCE:
                return out
            where = f"{loc.path}:{loc.line}" if loc.path and loc.line else loc.path or ""
            text = " ".join(x for x in (where, loc.code or "", loc.message) if x)
            out.append(
                EvidenceLine(
                    source="log", text=text[:_MAX_EVIDENCE_CHARS], path=loc.path, line=loc.line
                )
            )
    return out


def root_cause(decision: LabelDecision, step: str | None = None) -> str:
    """One or two sentences from the parsed errors."""
    sub = decision.sub_label or "build_other"
    what = TOOL_NAMES.get(sub, "the build step")
    findings = decision.findings
    if not findings:
        name = f"The step `{step}`" if step else "The build step"
        return (
            f"{name} failed, but its output matches no known tool (tsc, ESLint, Prettier, "
            "bundlers), so it is likely a project-specific check. The cited lines are its own "
            f"message (rule {decision.rule_name})."
        )[:1_000]
    errors = sum(f.errors for f in findings)
    codes = Counter(c for f in findings for c in f.codes.elements())
    files = sorted({loc.path for f in findings for loc in f.locations if loc.path})
    parts = [f"{what[0].upper()}{what[1:]} failed"]
    if errors:
        parts.append(f"with {errors} error{'s' if errors != 1 else ''}")
    if codes:
        parts.append("(" + ", ".join(f"{c} ×{n}" for c, n in codes.most_common(3)) + ")")
    if files:
        more = (
            f" and {len(files) - 1} other file{'s' if len(files) > 2 else ''}"
            if len(files) > 1
            else ""
        )
        parts.append(f"in {files[0]}{more}")
    text = " ".join(parts) + "."
    if any(f.config_error for f in findings):
        text += " The tool's own configuration failed to load."
    if any(f.max_warnings_exceeded for f in findings):
        text += " ESLint failed because the number of warnings exceeded --max-warnings."
    if decision.notes:
        text += f" Note: {decision.notes}."
    return text[:1_000]


def remediation(decision: LabelDecision, cases: list[RetrievedCase]) -> Remediation:
    """Steps for the tool, plus what fixed the most similar past failures (cited)."""
    sub = decision.sub_label or "build_other"
    steps = list(STEPS.get(sub, STEPS["build_other"]))
    for c in cases:
        steps.append(f"A similar failure was fixed before ({c.case_id}): {c.fix_summary}")
    summary = f"Fix the {ERROR_NOUN.get(sub, 'build')} errors at the cited locations" + (
        f"; {len(cases)} similar past fix(es) are listed." if cases else "."
    )
    return Remediation(
        summary=summary[:500], steps=steps, cites_case_ids=[c.case_id for c in cases]
    )


def _query(decision: LabelDecision) -> str:
    codes = " ".join(sorted({c for f in decision.findings for c in f.codes}))
    msgs = " ".join(loc.message for f in decision.findings for loc in f.locations[:5])
    return f"{decision.sub_label or 'build'} {codes} {msgs}"[:2_000]


def diagnose(failure: RoutedFailure, kb: CaseSearch | None = None) -> Diagnosis:
    """Turn a RoutedFailure into a Diagnosis (or a misrouted answer)."""
    started = time.perf_counter()
    text = _read_log(failure)
    signals = parse_job_log(text) if text.strip() else None
    decision = decide(_facts(failure), signals)
    evidence = _evidence(decision.findings) or _fallback_evidence(failure, text)
    if not decision.claimed:
        return Diagnosis(
            failure_id=failure.failure_id,
            expert=EXPERT,
            status="misrouted",
            suggested_category=_misrouted_category(decision, failure, signals),
            root_cause=f"Not a build or code-quality failure (rule {decision.rule_name}).",
            evidence=evidence,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    cases: list[RetrievedCase] = []
    if kb is not None:
        try:
            cases = kb.search(_query(decision), CASES_K, kind=decision.sub_label)
        except Exception as exc:  # noqa: BLE001 - retrieval is optional; diagnose without it
            logger.warning("case retrieval failed", extra={"error": str(exc)})
    step = failure.failed_steps[0].name.removeprefix("Run ")
    cause, fix = root_cause(decision, step), remediation(decision, cases)
    llm_model = prompt_version = None
    refined = refine_with_llm(failure, decision, cause, fix, cases, evidence)
    if refined is not None:
        cause, fix, llm_model, prompt_version = refined
    tool = decision.tool if decision.tool in ("tsc", "eslint", "bundler") else "other"
    return Diagnosis(
        failure_id=failure.failure_id,
        expert=EXPERT,
        status="diagnosed",
        root_cause=cause,
        confidence=CONFIDENCE.get(decision.confidence, 0.5),
        evidence=evidence,
        retrieved_cases=cases,
        remediation=fix,
        details=BuildDetails(tool=tool, locations=_locations(decision.findings)),
        llm_model=llm_model,
        prompt_version=prompt_version,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
