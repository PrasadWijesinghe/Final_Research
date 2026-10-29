"""POST /predict: the trained model when BUILD_MODEL_DIR names a bundle, else the P2.1 stub.

The stub (a stable low probability from a hash of head_sha) keeps the API working before a
model exists, so the platform and its tests run without one.
"""

import hashlib
import logging
import os
import time
from functools import cache

from shared.contracts import CommitContext, RiskOutput

from .config import EXPERT, MODEL_VERSION
from .serving import BuildPredictor, load_from_env

logger = logging.getLogger(__name__)

# DEFAULT: docs/experts/README.md fixes the stub operating threshold at 0.5.
THRESHOLD = 0.5


def stub_probability(head_sha: str) -> float:
    """Deterministic probability in [0.01, 0.2) derived from the SHA-256 of head_sha."""
    digest = hashlib.sha256(head_sha.encode("ascii")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    # DEFAULT: 0.01..0.2 keeps stubs far below the 0.5 threshold so they never alert.
    return round(0.01 + 0.19 * fraction, 4)


@cache
def predictor() -> BuildPredictor | None:
    """The loaded model (once per process), or None when no bundle is configured."""
    return load_from_env(dict(os.environ))


def current_model_version() -> str:
    """The loaded model's version, or "stub"."""
    p = predictor()
    return p.version if p is not None else MODEL_VERSION


def stub_predict(context: CommitContext) -> RiskOutput:
    """The P2.1 stub answer."""
    started = time.perf_counter()
    probability = stub_probability(context.head_sha)
    return RiskOutput(
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


def predict(context: CommitContext, deadline_ms: int | None = None) -> list[RiskOutput]:
    """Risk outputs for a commit (one commit-scoped item)."""
    model = predictor()
    logger.info(
        "prediction",
        extra={
            "context_id": str(context.context_id),
            "deadline_ms": deadline_ms,
            "model": model.version if model else MODEL_VERSION,
        },
    )
    if model is None:
        return [stub_predict(context)]
    return [model.predict(context)]
