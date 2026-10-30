"""FastAPI app: POST /complete and GET /health (port 8200, internal only)."""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import create_engine

from shared.db.session import get_database_url

from .config import load_config, provider_name, resolve_model
from .providers import get_provider
from .service import CompleteRequest, CompleteResponse, Gateway, GatewayError
from .stores import CacheStore, CallLog, InMemoryCache, InMemoryCallLog, RedisCache, SqlCallLog

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def default_gateway() -> Gateway:
    """Build the gateway from env: Redis cache and Postgres log when configured."""
    redis_url = os.environ.get("REDIS_URL", "").strip()
    cache: CacheStore = RedisCache(Redis.from_url(redis_url)) if redis_url else InMemoryCache()
    call_log: CallLog
    # DEFAULT: with no DATABASE_URL the log is in memory (lost on restart), with a warning.
    if os.environ.get("DATABASE_URL", "").strip():
        call_log = SqlCallLog(create_engine(get_database_url()))
    else:
        logger.warning("DATABASE_URL not set: llm_calls and spend are kept in memory only")
        call_log = InMemoryCallLog()
    config = load_config()
    provider = provider_name()
    return Gateway(
        get_provider(provider, config.provider_request),
        config,
        cache,
        call_log,
        model=resolve_model(config, provider),
    )


def create_app(gateway: Gateway | None = None) -> FastAPI:
    """Create the app; tests pass a Gateway built with in-memory stores."""
    app = FastAPI(title="PipelineIQ LLM gateway")
    app.state.gateway = gateway

    def current(request: Request) -> Gateway:
        gw: Gateway | None = request.app.state.gateway
        return gw if gw is not None else default_gateway()

    @app.exception_handler(GatewayError)
    async def _refusal(_request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body)

    @app.post("/complete")
    def post_complete(body: CompleteRequest, request: Request) -> CompleteResponse:
        """Validated JSON answer for the request (cached, capped, logged)."""
        return current(request).complete(body)

    @app.get("/health")
    def get_health() -> dict[str, str]:
        """Liveness plus the active provider."""
        return {"status": "ok", "provider": provider_name()}

    return app


app = create_app()
