"""Run setup: runner OS, matrix size, PR or push, default branch."""

from __future__ import annotations

from ._types import FeatureDict, JobTarget

FEATURE_NAMES = (
    "rs_runner_os",
    "rs_matrix_size",
    "rs_jobs_in_run",
    "rs_is_pull_request",
    "rs_event",
    "rs_on_default_branch",
    "rs_attempt",
)


def run_setup(t: JobTarget) -> FeatureDict:
    """Facts about how this run was triggered and where the job runs."""
    # DEFAULT: matrix size = jobs of the same key in this run (raw has no parsed workflow matrix).
    return {
        "rs_runner_os": t.runner_os,
        "rs_matrix_size": t.matrix_size,
        "rs_jobs_in_run": t.jobs_in_run,
        "rs_is_pull_request": int(bool(t.event and t.event.startswith("pull_request"))),
        "rs_event": t.event,
        "rs_on_default_branch": (
            None if not t.branch or not t.default_branch else int(t.branch == t.default_branch)
        ),
        "rs_attempt": t.attempt,
    }
