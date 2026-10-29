"""The no-LLM diagnosis text (P4.11): root cause and remediation built from triage and the KB.

Used when the gateway is unset, down, over its cap or returns an invalid answer, and also when the
gateway runs the mock provider (whose answers are placeholders). It only restates facts the triage
model and the knowledge base already produced; it never claims more than they say.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from testing_expert.triage.evidence import FAMILY_TEXT

from .prompt import CaseInfo

_LEAD = {
    "genuine": "Likely a real defect in this change, not flakiness.",
    "flaky": "Likely a flaky failure rather than a defect in this change.",
    "unclear": "The evidence does not separate a real defect from flakiness.",
}
_SUMMARY = {
    "genuine": (
        "Read the failing assertion and the changed code it points to, then fix the code, or "
        "update the test if the new behaviour is intended."
    ),
    "flaky": (
        "Re-run the failed job once; a pass on the same commit confirms it was flaky. If it "
        "fails again, fix the cause behind the error."
    ),
    "unclear": (
        "Re-run the failed job once and read the first failing assertion before deciding "
        "whether the change or the test is at fault."
    ),
}
# DEFAULT: the fix text of a past case is shown cut to 220 characters.
_FIX_CHARS = 220
# DEFAULT: a failing test name is shown cut to 120 characters.
_NAME_CHARS = 120
FLAKY_TAG = "kind:flaky"


@dataclass(frozen=True)
class Narrative:
    """Root cause, remediation and the one past case the text leans on (if any)."""

    root_cause: str
    summary: str
    steps: list[str]
    cites: list[str]


def best_case(verdict: str, cases: Sequence[CaseInfo]) -> CaseInfo | None:
    """The best-ranked case of the matching kind (flaky cases for a flaky verdict, real fixes
    otherwise); the first case when none matches the kind."""
    want_flaky = verdict == "flaky"
    for c in cases:
        if (FLAKY_TAG in c.tags) == want_flaky:
            return c
    return cases[0] if cases else None


def build(
    *,
    verdict: str,
    failing_tests: Sequence[str],
    families: Sequence[str],
    leaning_reasons: Sequence[str],
    touched_path: str | None,
    suggest_rerun: bool,
    cases: Sequence[CaseInfo],
    min_similarity: float = 0.6,
) -> Narrative:
    """Plain-language narrative for the verdict, using at most one past case."""
    parts = [_LEAD[verdict]]
    if failing_tests:
        more = f" and {len(failing_tests) - 1} more" if len(failing_tests) > 1 else ""
        what = next((FAMILY_TEXT[f] for f in families if f in FAMILY_TEXT), None)
        name = failing_tests[0]
        name = name if len(name) <= _NAME_CHARS else name[: _NAME_CHARS - 3] + "..."
        parts.append(
            f'The failing test "{name}"{more}' + (f" failed with {what}." if what else " failed.")
        )
    parts += [r.rstrip() for r in leaning_reasons[:2]]
    steps: list[str] = []
    if suggest_rerun:
        steps.append("Re-run the failed job once; a pass on the same commit confirms it was flaky.")
    if verdict != "flaky" and touched_path:
        steps.append(f"Compare this commit's changes to {touched_path} with the failing assertion.")
    elif verdict != "flaky":
        steps.append("Compare the expected and actual values in the first failing assertion.")
    case = best_case(verdict, [c for c in cases if c.similarity >= min_similarity])
    cites: list[str] = []
    if case is not None:
        fix = " ".join(case.fix_summary.split())[:_FIX_CHARS]
        steps.append(f"The closest past case ({case.case_id}) was resolved like this: {fix}")
        cites.append(case.case_id)
    return Narrative(" ".join(parts)[:1_000], _SUMMARY[verdict][:500], steps, cites)
