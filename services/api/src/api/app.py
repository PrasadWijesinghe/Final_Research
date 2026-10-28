"""FastAPI app for the backend (port 8000). Endpoints are listed in docs/ARCHITECTURE.md 4."""

# DEFAULT: no `from __future__ import annotations` here: FastAPI resolves the Depends(...) of the
# locally defined auth helpers from the real annotation objects.

import asyncio
import json
import logging
import os
import time
import uuid
from functools import lru_cache
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from features.pre_push import PrePushPayload, build_pre_push_context
from sqlalchemy import create_engine

from shared.contracts import Diagnosis, ReviewDecision
from shared.db.session import get_database_url
from shared.github import InstallationTokenCache, verify_signature

from .auth import generate_key, hash_key, parse_bearer, tokens_equal
from .config import Settings
from .experts import fan_out_predict, forward_review, probe_health
from .raw_store import SqlRawStore
from .router import FailedJob, route_failed_job
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
from .webhooks import WebhookHandler

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def default_store() -> Store:
    """Postgres store from DATABASE_URL (raises a clear error when it is not set)."""
    return SqlStore(create_engine(get_database_url()))


def _route_to_queue(job: FailedJob) -> None:
    """Route a failed job and queue it for the experts (Redis from REDIS_URL)."""
    from .failure_queue import redis_from_env

    route_failed_job(job, default_store(), redis_from_env())


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    webhooks: WebhookHandler | None = None,
) -> FastAPI:
    """Create the app; tests pass an in-memory store and settings pointing at respx routes."""
    app = FastAPI(title="PipelineIQ backend")
    cfg = settings or Settings()
    handler = webhooks

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

    # ------------------------------------------------------------- GitHub App (P3.1)

    def get_handler() -> WebhookHandler:
        nonlocal handler
        if handler is None:
            if not (cfg.github_app_id and cfg.github_app_key_path):
                raise HTTPException(503, "GitHub App is not configured")
            engine = create_engine(get_database_url())
            handler = WebhookHandler(
                cfg,
                get_store(),
                SqlRawStore(engine),
                InstallationTokenCache(
                    cfg.github_app_id, os.path.expanduser(cfg.github_app_key_path)
                ),
                _route_to_queue,
            )
        return handler

    async def run_webhook(event: str, delivery: str, payload: dict[str, Any]) -> None:
        """Background job: errors are logged (never raised, never with the payload)."""
        try:
            outcome = await get_handler().handle(event, payload)
            logger.info(
                "webhook handled", extra={"event": event, "delivery": delivery, "outcome": outcome}
            )
        except Exception:
            logger.exception("webhook failed", extra={"event": event, "delivery": delivery})

    @app.post("/webhooks/github", status_code=202)
    async def post_webhook(
        request: Request,
        background: BackgroundTasks,
        x_hub_signature_256: Annotated[str | None, Header()] = None,
        x_github_event: Annotated[str | None, Header()] = None,
        x_github_delivery: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        """Verify the signature, then handle the event in the background (GitHub waits 10 s)."""
        if not cfg.github_webhook_secret:
            raise HTTPException(503, "webhooks are disabled: GITHUB_WEBHOOK_SECRET is not set")
        body = await request.body()
        if not verify_signature(cfg.github_webhook_secret, body, x_hub_signature_256):
            logger.warning("webhook signature mismatch", extra={"delivery": x_github_delivery})
            raise HTTPException(401, "invalid signature")
        event = x_github_event or ""
        if event == "ping":
            return {"status": "pong"}
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "body is not a JSON object")
        background.add_task(run_webhook, event, x_github_delivery or "", payload)
        return {"status": "accepted", "event": event}

    @app.api_route("/auth/github/{rest:path}", methods=["GET", "POST"], status_code=501)
    async def auth_github(rest: str) -> dict[str, str]:
        """Not built yet (OAuth sign-in is left to the dashboard task, P3.5)."""
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
