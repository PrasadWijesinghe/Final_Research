"""FastAPI app: POST /predict, POST /review, GET /health (interface in docs/experts/README.md)."""

import logging
from typing import Annotated

from fastapi import FastAPI, Header, Response
from pydantic import BaseModel

from shared.contracts import CommitContext, ReviewDecision, RiskOutput

from .config import EXPERT, MODEL_VERSION
from .predict import predict
from .serving import get_bundle, serve

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
    """Risk outputs from the trained model; the stub answers when no model bundle is loaded."""
    return serve(context) or predict(context, deadline_ms=_parse_deadline(x_deadline_ms))


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
    """Liveness plus model version and knowledge-base size (kb_cases is 0 until a KB exists)."""
    bundle = get_bundle()
    return Health(
        status="ok", model_version=bundle.version if bundle else MODEL_VERSION, kb_cases=0
    )
