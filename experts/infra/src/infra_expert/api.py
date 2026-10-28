"""FastAPI app: POST /predict, POST /review, GET /health (interface in docs/experts/README.md)."""

import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from threading import RLock
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from shared.contracts import CommitContext, ReviewDecision, RiskOutput

from .config import EXPERT, MODEL_VERSION
from .model import configured_model_path, load_model
from .monitor import controller
from .monitor import router as monitor_router
from .predict import RESEARCH_WARNING, predict

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    yield
    controller.stop()


app = FastAPI(title=f"PipelineIQ {EXPERT.value} expert", lifespan=lifespan)
app.include_router(monitor_router)


class Health(BaseModel):
    """Body of GET /health."""

    status: str
    model_version: str
    kb_cases: int


class LatestPushPrediction(BaseModel):
    """Last prediction received by this process, for the local push demonstration."""

    repo: str
    branch: str
    head_sha: str
    base_sha: str | None
    changed_files: list[str]
    received_at: datetime
    prediction: RiskOutput
    source: Literal["push", "pre_push"]
    initial_snapshot: bool = False


_latest_push: LatestPushPrediction | None = None
# DEFAULT: retain the 50 latest live predictions in this single local API process.
_push_history: deque[LatestPushPrediction] = deque(maxlen=50)
_prediction_lock = RLock()


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
    """Return the research model's Infra failure estimate for a commit."""
    outputs = predict(context, deadline_ms=_parse_deadline(x_deadline_ms))
    return outputs


@app.post("/monitor/predict")
def post_monitored_prediction(context: CommitContext) -> list[RiskOutput]:
    """Publish a real remote revision or a local hook event; manual predictions stay separate."""
    with _prediction_lock:
        return _record_monitored_prediction(context)


def _record_monitored_prediction(context: CommitContext) -> list[RiskOutput]:
    global _latest_push
    if context.event not in {"push", "pre_push"}:
        raise HTTPException(422, "Monitoring accepts only push and pre_push contexts.")
    source: Literal["push", "pre_push"] = "push" if context.event == "push" else "pre_push"
    # A retry after a lost HTTP response returns the already-computed prediction.
    for previous in _push_history:
        if (
            previous.repo,
            previous.branch,
            previous.head_sha,
            previous.base_sha,
            previous.source,
        ) == (
            f"{context.repo.owner}/{context.repo.name}",
            context.branch,
            context.head_sha,
            context.base_sha,
            source,
        ):
            return [previous.prediction]
    outputs = predict(context)
    _latest_push = LatestPushPrediction(
        repo=f"{context.repo.owner}/{context.repo.name}",
        branch=context.branch,
        head_sha=context.head_sha,
        base_sha=context.base_sha,
        changed_files=[file.path for file in context.changed_files],
        received_at=datetime.now(UTC),
        prediction=outputs[0],
        source=source,
        initial_snapshot=context.base_sha is None,
    )
    _push_history.appendleft(_latest_push)
    return outputs


@app.get("/demo/latest", response_model=LatestPushPrediction | None)
def get_latest_push() -> LatestPushPrediction | None:
    """Return the latest in-memory prediction so the local dashboard can follow a git push."""
    return _latest_push


@app.get("/monitor/history", response_model=list[LatestPushPrediction])
def get_push_history() -> list[LatestPushPrediction]:
    """Return recent live predictions, newest first, for the current API session."""
    with _prediction_lock:
        return list(_push_history)


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
    """Report model availability; degraded means available but not production-validated."""
    try:
        load_model()
    except (FileNotFoundError, OSError, ValueError):
        logger.exception(
            "Infra model could not be loaded", extra={"path": str(configured_model_path())}
        )
        return Health(status="error", model_version=MODEL_VERSION, kb_cases=0)
    logger.info("Infra model is research-only", extra={"warning": RESEARCH_WARNING})
    return Health(status="degraded", model_version=MODEL_VERSION, kb_cases=0)
