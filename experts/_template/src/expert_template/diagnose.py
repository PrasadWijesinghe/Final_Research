"""Stub diagnosis: fixed text plus one evidence line from the log excerpt. No LLM call yet."""

import time

from shared.contracts import Diagnosis, EvidenceLine, Remediation, RoutedFailure

from .config import EXPERT, make_details

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


def diagnose(failure: RoutedFailure) -> Diagnosis:
    """Turn a RoutedFailure into a valid stub Diagnosis."""
    started = time.perf_counter()
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
