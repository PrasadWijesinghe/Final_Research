"""FastAPI app: POST /predict, POST /review, GET /health (interface in docs/experts/README.md).

Extra endpoints for the Build page of the dashboard (`services/dashboard/src/build/`), all
read-only and outside the shared interface: POST /diagnose runs the diagnosis worker's code on
one RoutedFailure now, GET /model gives the loaded model's test results, and GET /examples lists
the demo kit's commits and failed jobs when BUILD_EXAMPLES_DIR is set.
"""

import logging
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from shared.contracts import CommitContext, Diagnosis, ReviewDecision, RiskOutput, RoutedFailure

from . import predict as prediction
from .config import EXPERT
from .diagnose import diagnose
from .predict import current_model_version, predict
from .rag.kb import default_kb
from .showcase import examples_dir, list_examples, model_summary, read_example

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


@app.post("/diagnose")
def post_diagnose(failure: RoutedFailure) -> Diagnosis:
    """Diagnose one failed job now, with the same code the queue worker runs."""
    return diagnose(failure, kb=default_kb())


@app.get("/model")
def get_model() -> dict[str, Any]:
    """The loaded model's version, threshold and test results ("stub" when none is loaded)."""
    return model_summary(prediction.predictor())


@app.get("/examples")
def get_examples() -> list[dict[str, str]]:
    """The demo kit's commits and failed jobs (empty unless BUILD_EXAMPLES_DIR is set)."""
    root = examples_dir()
    return list_examples(root) if root else []


@app.get("/examples/{kind}/{name}")
def get_example(kind: str, name: str) -> Any:
    """One example as JSON: a CommitContext (commits) or a RoutedFailure (failures)."""
    root = examples_dir()
    data = read_example(root, kind, name) if root else None
    if data is None:
        raise HTTPException(status_code=404, detail="no such example")
    return data
