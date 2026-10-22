"""FastAPI app for the backend (port 8000). Endpoints are listed in docs/ARCHITECTURE.md 4."""

# DEFAULT: no `from __future__ import annotations` here: FastAPI resolves the Depends(...) of the
# locally defined auth helpers from the real annotation objects.

import asyncio
import logging
import time
import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from features.pre_push import PrePushPayload, build_pre_push_context
from sqlalchemy import create_engine

from shared.contracts import Diagnosis, ReviewDecision
from shared.db.session import get_database_url

from .auth import generate_key, hash_key, parse_bearer, tokens_equal
from .config import Settings
from .experts import fan_out_predict, forward_review, probe_health
from .schemas import (
    ApiKeyIssued,
    ApiKeyRequest,
    CommitRisks,
    FailureView,
    HealthResponse,
    PredictResponse,
    ReviewAck,
    RunItem,
)
from .store import KeyRecord, SqlStore, Store

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def default_store() -> Store:
    """Postgres store from DATABASE_URL (raises a clear error when it is not set)."""
    return SqlStore(create_engine(get_database_url()))


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    """Create the app; tests pass an in-memory store and settings pointing at respx routes."""
    app = FastAPI(title="PipelineIQ backend")
    cfg = settings or Settings()

    def get_store() -> Store:
        return store if store is not None else default_store()

    async def current_key(
        authorization: Annotated[str | None, Header()] = None,
    ) -> KeyRecord:
        token = parse_bearer(authorization)
        if token is None:
            raise HTTPException(401, "missing Bearer API key")
        key = await asyncio.to_thread(get_store().find_active_key, hash_key(token))
        if key is None:
            raise HTTPException(401, "invalid or revoked API key")
        return key

    async def require_admin(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if not cfg.admin_token:
            raise HTTPException(503, "key management is disabled: API_ADMIN_TOKEN is not set")
        token = parse_bearer(authorization)
        if token is None or not tokens_equal(token, cfg.admin_token):
            raise HTTPException(401, "invalid admin token")

    # ------------------------------------------------------------------ predictions

    @app.post("/predict")
    async def post_predict(
        payload: PrePushPayload, key: Annotated[KeyRecord, Depends(current_key)]
    ) -> PredictResponse:
        """Pre-push: build the context, ask all four experts within 10 s, save, combine."""
        started = time.monotonic()
        db = get_store()
        context = await asyncio.to_thread(
            build_pre_push_context, payload, author_hash=key.user_hash
        )
        await asyncio.to_thread(db.save_context, context)
        results = await fan_out_predict(cfg, context, cfg.pre_push_budget_ms)
        ready = [risk for r in results for risk in r.risks]
        if ready:
            await asyncio.to_thread(db.save_predictions, ready)
        logger.info(
            "predict done",
            extra={
                "context_id": str(context.context_id),
                "not_ready": [r.expert.value for r in results if r.state == "not_ready"],
            },
        )
        return PredictResponse(
            context_id=context.context_id,
            head_sha=context.head_sha,
            budget_ms=cfg.pre_push_budget_ms,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            results=results,
        )

    @app.get("/commits/{sha}/risks")
    async def get_commit_risks(
        sha: str, repo: str | None = None, include_replay: bool = False
    ) -> CommitRisks:
        """Stored risks for a commit (replay results only when asked for)."""
        risks = await asyncio.to_thread(
            get_store().risks_for_commit, sha, repo=repo, include_replay=include_replay
        )
        return CommitRisks(head_sha=sha, risks=risks)

    @app.get("/runs")
    async def get_runs(repo: str | None = None, limit: int = 50) -> list[RunItem]:
        """History: predictions next to the actual outcome of the run."""
        # DEFAULT: newest 50 contexts, hard cap 200 per request.
        limit = max(1, min(limit, 200))
        return await asyncio.to_thread(get_store().list_runs, repo=repo, limit=limit)

    # -------------------------------------------------------- diagnoses and reviews

    @app.post("/diagnoses", status_code=201)
    async def post_diagnosis(diagnosis: Diagnosis) -> dict[str, str]:
        """Expert workers post a finished Diagnosis here."""
        # DEFAULT: unauthenticated; the endpoint is for workers on the internal network (like
        # the LLM gateway). Secure it with a shared worker token before any public deployment.
        await asyncio.to_thread(get_store().save_diagnosis, diagnosis)
        return {"diagnosis_id": str(diagnosis.diagnosis_id)}

    @app.post("/reviews", status_code=201)
    async def post_review(decision: ReviewDecision) -> ReviewAck:
        """Save a developer's decision and forward it to the expert that wrote the diagnosis."""
        db = get_store()
        diagnosis = await asyncio.to_thread(db.get_diagnosis, decision.diagnosis_id)
        if diagnosis is None:
            raise HTTPException(404, "diagnosis not found")
        await asyncio.to_thread(db.save_review, decision)
        forwarded = await forward_review(cfg, diagnosis.expert, decision)
        return ReviewAck(decision_id=decision.decision_id, forwarded=forwarded)

    @app.get("/failures/{failure_id}")
    async def get_failure(failure_id: uuid.UUID) -> FailureView:
        """A routed failure with its diagnoses and reviews."""
        found = await asyncio.to_thread(get_store().get_failure, failure_id)
        if found is None:
            raise HTTPException(404, "failure not found")
        failure, diagnoses, reviews = found
        return FailureView(failure=failure, diagnoses=diagnoses, reviews=reviews)

    # ---------------------------------------------------------------------- API keys

    @app.post("/api-keys", status_code=201, dependencies=[Depends(require_admin)])
    async def post_api_key(body: ApiKeyRequest) -> ApiKeyIssued:
        """Issue a pre-push key. The plain key is returned here once and never stored."""
        plain = generate_key()
        record = await asyncio.to_thread(get_store().add_key, body.user_hash, hash_key(plain))
        return ApiKeyIssued(id=record.id, key=plain, created_at=record.created_at)

    @app.delete("/api-keys/{key_id}", status_code=204, dependencies=[Depends(require_admin)])
    async def delete_api_key(key_id: uuid.UUID) -> Response:
        """Revoke a key."""
        if not await asyncio.to_thread(get_store().revoke_key, key_id):
            raise HTTPException(404, "key not found")
        return Response(status_code=204)

    # ----------------------------------------------------- GitHub App (Phase 3) stubs

    @app.post("/webhooks/github", status_code=501)
    async def post_webhook(request: Request) -> dict[str, str]:
        """Not built until P3.1."""
        return {"detail": "GitHub webhooks are not implemented yet (Phase 3)"}

    @app.api_route("/auth/github/{rest:path}", methods=["GET", "POST"], status_code=501)
    async def auth_github(rest: str) -> dict[str, str]:
        """Not built until P3.1."""
        return {"detail": "GitHub sign-in is not implemented yet (Phase 3)"}

    # ------------------------------------------------------------------------ health

    @app.get("/health")
    async def get_health() -> HealthResponse:
        """Backend status plus each expert's."""
        experts = await probe_health(cfg)
        down = any(h.status != "ok" for h in experts.values())
        return HealthResponse(status="degraded" if down else "ok", experts=experts)

    return app


app = create_app()
