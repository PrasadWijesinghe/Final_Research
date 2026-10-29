"""Collect the evidence of one failure: the one code path training and serving both use (P4.9).

`gather(conn, ref, log_text, prerun)` reads the log, then the database as of the failure's time
(`loader`), and returns a `TriageEvidence`. Training calls it for every labelled failure with the
failed job's `completed_at` as cutoff; the worker calls it for a live failure. Same function, so
the features cannot differ between the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import loader
from .evidence import history_stats, overlap, same_sha_stats
from .features import LogEvidence, TriageEvidence

# DEFAULT: the sanitised log tail the router sends is at most 20 000 characters
# (RoutedFailure.log_excerpt); training cuts full logs to the same length so both see alike text.
EXCERPT_CHARS = 20_000


@dataclass(frozen=True)
class FailureRef:
    """Which failure, and as of when. `cutoff` is the failed job's `completed_at`."""

    repo: str
    head_sha: str
    job_id: int
    run_attempt: int
    job_name: str | None
    workflow_path: str
    cutoff: datetime


def gather(
    conn: loader.Conn | None,
    ref: FailureRef,
    log: LogEvidence,
    prerun_flaky: float | None = None,
) -> TriageEvidence:
    """Evidence for one failure. Without a connection (or an unknown repo) the database-backed
    parts are unknown rather than zero."""
    repo_id = loader.repo_id_of(conn, ref.repo) if conn is not None else None
    if conn is None or repo_id is None:
        return TriageEvidence(
            log=log,
            overlap=overlap(log.failures, None),
            history=history_stats(len(log.test_ids), []),
            same_sha=None,
            prerun_flaky=prerun_flaky,
        )
    files = loader.commit_files(conn, repo_id, ref.head_sha)
    sibs = loader.siblings(conn, repo_id, ref.head_sha, ref.job_id, ref.cutoff)
    prior = loader.prior_tests(conn, repo_id, log.test_ids, ref.job_id, ref.cutoff)
    return TriageEvidence(
        log=log,
        overlap=overlap(log.failures, files),
        history=history_stats(len(log.test_ids), prior),
        same_sha=same_sha_stats(
            ref.run_attempt, ref.job_name, ref.workflow_path, sibs, ref.cutoff.timestamp()
        ),
        prerun_flaky=prerun_flaky,
    )
