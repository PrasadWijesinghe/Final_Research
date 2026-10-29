"""FastAPI app: POST /predict, POST /review, GET /health (interface in docs/experts/README.md)."""

import logging
from typing import Annotated

from fastapi import FastAPI, Header, Response
from pydantic import BaseModel

from shared.contracts import CommitContext, ReviewDecision, RiskOutput

from .config import EXPERT
from .predict import current_model_version, predict
from .rag.kb import default_kb

logger = logging.getLogger(__name__)

app = FastAPI(title=f"PipelineIQ {EXPERT.value} expert")


class Health(BaseModel):
    """Body of GET /health."""

    status: str
    model_version: str
    kb_cases: int


def _parse_deadline(raw: str | None) -> int | None:
    """Read the X-Deadline-Ms header; anything unparseable counts as no deadline."""
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


@app.post("/predict")
def post_predict(
    context: CommitContext,
    x_deadline_ms: Annotated[str | None, Header()] = None,
) -> list[RiskOutput]:
    """Risk outputs for a commit. Scoring takes milliseconds, so the deadline is only logged."""
    return predict(context, deadline_ms=_parse_deadline(x_deadline_ms))


@app.post("/review", status_code=204)
def post_review(decision: ReviewDecision) -> Response:
    """Accept a developer's review decision (stub: log it only, never the comment text)."""
    logger.info(
        "review received",
        extra={
            "decision_id": str(decision.decision_id),
            "diagnosis_id": str(decision.diagnosis_id),
            "decision": decision.decision,
        },
    )
    return Response(status_code=204)


@app.get("/health")
def get_health() -> Health:
    """Liveness plus model version and knowledge-base size (0 when no KB is reachable)."""
    kb = default_kb()
    return Health(
        status="ok", model_version=current_model_version(), kb_cases=kb.count() if kb else 0
    )
