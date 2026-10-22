"""Stub prediction: a stable low probability from a hash of head_sha. Replace with the model."""

import hashlib
import logging
import time

from shared.contracts import CommitContext, RiskOutput

from .config import EXPERT, MODEL_VERSION

logger = logging.getLogger(__name__)

# DEFAULT: docs/experts/README.md fixes the stub operating threshold at 0.5.
THRESHOLD = 0.5


def stub_probability(head_sha: str) -> float:
    """Deterministic probability in [0.01, 0.2) derived from the SHA-256 of head_sha."""
    digest = hashlib.sha256(head_sha.encode("ascii")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    # DEFAULT: 0.01..0.2 keeps stubs far below the 0.5 threshold so they never alert.
    return round(0.01 + 0.19 * fraction, 4)


def predict(context: CommitContext, deadline_ms: int | None = None) -> list[RiskOutput]:
    """Return the stub RiskOutput list for a commit (one commit-scoped item)."""
    started = time.perf_counter()
    probability = stub_probability(context.head_sha)
    logger.info(
        "stub prediction",
        extra={"context_id": str(context.context_id), "deadline_ms": deadline_ms},
    )
    return [
        RiskOutput(
            context_id=context.context_id,
            expert=EXPERT,
            model_version=MODEL_VERSION,
            scope="commit",
            status="ok",
            probability=probability,
            threshold=THRESHOLD,
            alert=probability >= THRESHOLD,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    ]
