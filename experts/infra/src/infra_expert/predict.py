"""Infrastructure failure prediction backed by the packaged research model."""

from __future__ import annotations

import logging
import time

from shared.contracts import CommitContext, RiskFactor, RiskOutput

from .config import EXPERT, MODEL_VERSION
from .model import load_model

logger = logging.getLogger(__name__)

RESEARCH_WARNING = (
    "Research estimate only: this model has not passed unseen-repository validation. "
    "A low score does not establish that the workflow is safe."
)


def _threshold_factor(probability: float, threshold: float) -> RiskFactor:
    """Satisfy the alert contract without presenting the score as a root cause."""
    return RiskFactor(
        feature="research_model_score",
        value=round(probability, 6),
        shap_value=max(probability - threshold, 1e-9),
        direction="raises",
        reason="The research score exceeded its validation threshold; this is not a cause.",
    )


def predict(context: CommitContext, deadline_ms: int | None = None) -> list[RiskOutput]:
    """Return one calibrated commit-level Infra risk estimate."""
    started = time.perf_counter()
    model = load_model()
    probability = model.predict_probability(context)
    threshold = model.threshold
    alert = probability >= threshold
    warning = RESEARCH_WARNING
    workflows = {job.workflow_path for job in context.workflow_jobs}
    if len(workflows) > 1:
        warning += (
            f" Inputs combine {len(workflows)} workflows, but training examples represent "
            "individual workflow runs; this is not a calibrated commit failure probability."
        )
    logger.info(
        "research model prediction",
        extra={
            "context_id": str(context.context_id),
            "deadline_ms": deadline_ms,
            "model_version": MODEL_VERSION,
        },
    )
    return [
        RiskOutput(
            context_id=context.context_id,
            expert=EXPERT,
            model_version=MODEL_VERSION,
            scope="commit",
            status="degraded",
            error_message=warning,
            probability=probability,
            threshold=threshold,
            alert=alert,
            top_factors=[_threshold_factor(probability, threshold)] if alert else [],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    ]
