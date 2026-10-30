"""FastAPI app: POST /predict, POST /review, GET /health, GET /status (docs/experts/README.md)."""

import logging
from typing import Annotated

from fastapi import FastAPI, Header, Response
from pydantic import BaseModel

from shared.contracts import CommitContext, ReviewDecision, RiskOutput

from .config import EXPERT, MODEL_VERSION
from .kb.retrieve import count_cases
from .predict import predict
from .serving import get_bundle, serve
from .status import readiness

logger = logging.getLogger(__name__)

app = FastAPI(title=f"PipelineIQ {EXPERT.value} expert")


class Health(BaseModel):
    """Body of GET /health."""

    status: str
    model_version: str
    kb_cases: int


class Status(BaseModel):
    """Body of GET /status: what is real and what is a fallback in this process."""

    mode: str  # real | partial | stub
    model_version: str | None
    triage_version: str | None
    kb_cases: int
    llm_gateway: bool
    full_logs: bool


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
    """Liveness plus model version and knowledge-base size (0 when the database cannot be read)."""
    bundle = get_bundle()
    return Health(
        status="ok",
        model_version=bundle.version if bundle else MODEL_VERSION,
        kb_cases=count_cases(),
    )


@app.get("/status")
def get_status() -> Status:
    """Which parts are real (model, triage, knowledge base, gateway URL, full logs) vs the stub.

    # DEFAULT: a separate endpoint, so /health keeps the exact body in docs/experts/README.md.
    """
    state = readiness()
    return Status(
        mode=state.mode,
        model_version=state.model_version,
        triage_version=state.triage_version,
        kb_cases=state.kb_cases,
        llm_gateway=state.llm_gateway,
        full_logs=state.full_logs,
    )
