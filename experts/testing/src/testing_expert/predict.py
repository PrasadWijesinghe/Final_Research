"""Stub prediction: a stable low probability from a hash of head_sha. Replace with the model."""

import hashlib
import logging
import re
import time

from shared.contracts import CommitContext, RiskOutput, WorkflowJob

from .config import EXPERT, MODEL_VERSION

logger = logging.getLogger(__name__)

# DEFAULT: docs/experts/README.md fixes the stub operating threshold at 0.5.
THRESHOLD = 0.5

# DEFAULT: a "test job" is any job with a `run:` command that looks like a Node test runner
# (npm/yarn/pnpm test, jest, mocha, vitest, ...). Simple on purpose; the real model replaces it.
_TEST_COMMAND = re.compile(
    r"\b(?:(?:npm|yarn|pnpm)\s+(?:run\s+)?test(?::\S+)?|npx\s+(?:jest|mocha|vitest)|jest|mocha|vitest)\b"
)


def stub_probability(head_sha: str) -> float:
    """Deterministic probability in [0.01, 0.2) derived from the SHA-256 of head_sha."""
    digest = hashlib.sha256(head_sha.encode("ascii")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    # DEFAULT: 0.01..0.2 keeps stubs far below the 0.5 threshold so they never alert.
    return round(0.01 + 0.19 * fraction, 4)


def is_test_job(job: WorkflowJob) -> bool:
    """True when any of the job's `run:` commands looks like a test run."""
    return any(_TEST_COMMAND.search(command) for command in job.commands)


def predict(context: CommitContext, deadline_ms: int | None = None) -> list[RiskOutput]:
    """Return stub RiskOutputs: one per test job, or one commit-scoped item if none is found."""
    started = time.perf_counter()
    probability = stub_probability(context.head_sha)
    test_jobs = [job for job in context.workflow_jobs if is_test_job(job)]
    logger.info(
        "stub prediction",
        extra={
            "context_id": str(context.context_id),
            "deadline_ms": deadline_ms,
            "test_jobs": len(test_jobs),
        },
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    def _output(job: WorkflowJob | None) -> RiskOutput:
        # DEFAULT: every job of a commit gets the same stub probability (hash of head_sha only).
        return RiskOutput(
            context_id=context.context_id,
            expert=EXPERT,
            model_version=MODEL_VERSION,
            scope="job" if job else "commit",
            workflow_path=job.workflow_path if job else None,
            job_key=job.job_key if job else None,
            status="ok",
            probability=probability,
            threshold=THRESHOLD,
            alert=probability >= THRESHOLD,
            latency_ms=latency_ms,
        )

    return [_output(job) for job in test_jobs] or [_output(None)]
