"""Diagnosis of a failed test job.

The verdict part is real (P4.9 triage): genuine, flaky or unclear, a confidence, `suggest_rerun`
(only for a confident flaky verdict) and evidence lines from the log, the diff, the failing tests'
history, same-commit jobs and the pre-run risk. The narrative (root cause and remediation text) is
still the fixed stub text until the LLM step (P4.11). When triage cannot run (no triage bundle, or
the log shows no Jest, Mocha or Vitest failure) the whole diagnosis is the stub, as before.
"""

import logging
import time

from shared.contracts import (
    BehaviourDetails,
    Diagnosis,
    EvidenceLine,
    Remediation,
    RoutedFailure,
)

from .config import EXPERT, make_details
from .triage.features import Reason
from .triage.runtime import TriageResult, triage

logger = logging.getLogger(__name__)

# DEFAULT: the stub claims no confidence, since nothing was analysed.
STUB_CONFIDENCE = 0.0
STUB_ROOT_CAUSE = "Stub expert: no analysis was done. This is placeholder text."
STUB_REMEDIATION = "Stub expert: no fix suggested. Read the cited log line and the full log."
_MAX_EVIDENCE_CHARS = 1_000  # EvidenceLine.text limit in the contract


def _evidence_text(failure: RoutedFailure) -> str:
    """Last non-empty line of the log excerpt, or the failed step's name if the log is empty."""
    lines = [line.strip() for line in failure.log_excerpt.splitlines() if line.strip()]
    text = lines[-1] if lines else f"failed step: {failure.failed_steps[0].name}"
    return text[:_MAX_EVIDENCE_CHARS]


# EvidenceLine.source only knows log / stack_trace / diff / config (contract v1.1.0), so history,
# same-commit and pre-run evidence are logged as "log" lines with a short prefix.
# DEFAULT: proposal for the next contract version is an extra source value (see the report).
_PREFIX = {"history": "History: ", "same_sha": "Same commit: ", "pre_run": "Pre-run model: "}
_SOURCE = {"history": "log", "same_sha": "log", "pre_run": "log"}
# DEFAULT: at most 12 evidence lines per diagnosis (the strongest first).
MAX_EVIDENCE = 12
_VERDICT_TEXT = {
    "genuine": "Triage: this looks like a real bug in the change, not flakiness.",
    "flaky": "Triage: this looks like a flaky failure, not a bug in the change.",
    "unclear": "Triage: the evidence does not separate a real bug from flakiness.",
}


def _evidence_line(r: Reason) -> EvidenceLine:
    lean = f" [points to {r.leans}]" if r.leans != "neutral" else ""
    return EvidenceLine(
        source=_SOURCE.get(r.source, r.source),  # type: ignore[arg-type]
        text=(_PREFIX.get(r.source, "") + r.text + lean)[:1_000],
        path=r.path,
        line=r.line,
    )


def _triaged(failure: RoutedFailure, t: TriageResult, started: float) -> Diagnosis:
    names = [f.full_name or (f.file or "") for f in t.evidence.log.failures]
    names = [n for n in names if n] or [failure.failed_steps[0].name]
    risk = t.evidence.prerun_flaky
    details = BehaviourDetails(
        verdict=t.verdict,
        verdict_confidence=t.confidence,
        framework=t.framework,
        failing_tests=names[:20],
        stack_touches_changed_code=t.evidence.overlap.touches_changed_code,
        pre_run_flaky_risk=risk if risk is not None and 0.0 <= risk <= 1.0 else None,
        suggest_rerun=t.suggest_rerun,
    )
    steps = (
        ["Re-run the failed job once; a pass on the same commit confirms it was flaky."]
        if t.suggest_rerun
        else []
    )
    return Diagnosis(
        failure_id=failure.failure_id,
        expert=EXPERT,
        status="diagnosed",
        root_cause=f"{_VERDICT_TEXT[t.verdict]} {STUB_ROOT_CAUSE}"[:1_000],
        confidence=t.confidence,
        evidence=[_evidence_line(r) for r in t.reasons[:MAX_EVIDENCE]],
        retrieved_cases=[],
        remediation=Remediation(summary=STUB_REMEDIATION, steps=steps),
        details=details,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def diagnose(failure: RoutedFailure) -> Diagnosis:
    """Triage the failure; fall back to the stub Diagnosis when triage cannot run."""
    started = time.perf_counter()
    try:
        result = triage(failure)
    except Exception:
        logger.warning("triage failed, answering with the stub", exc_info=True)
        result = None
    if result is not None and result.reasons:
        return _triaged(failure, result, started)
    return _stub(failure, started)


def _stub(failure: RoutedFailure, started: float) -> Diagnosis:
    """The placeholder diagnosis: fixed text and the last log line."""
    return Diagnosis(
        failure_id=failure.failure_id,
        expert=EXPERT,
        status="diagnosed",
        root_cause=STUB_ROOT_CAUSE,
        confidence=STUB_CONFIDENCE,
        evidence=[EvidenceLine(source="log", text=_evidence_text(failure))],
        retrieved_cases=[],
        remediation=Remediation(summary=STUB_REMEDIATION),
        details=make_details(failure),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
