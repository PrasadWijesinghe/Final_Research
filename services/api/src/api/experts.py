"""Calls to the four experts: parallel /predict fan-out, /review forwarding and /health probes."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from pydantic import TypeAdapter, ValidationError

from shared.contracts import CommitContext, Expert, ReviewDecision, RiskOutput

from .config import Settings
from .schemas import ExpertHealth, ExpertResult

logger = logging.getLogger(__name__)

_RISKS = TypeAdapter(list[RiskOutput])
_JSON = {"Content-Type": "application/json"}


def _not_ready(expert: Expert, reason: str, started: float) -> ExpertResult:
    return ExpertResult(
        expert=expert,
        state="not_ready",
        reason=reason,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


async def _predict_one(
    settings: Settings, expert: Expert, context: CommitContext, budget_ms: int
) -> ExpertResult:
    """Ask one expert; never raises, any failure becomes a 'not_ready' result."""
    started = time.monotonic()
    url = f"{settings.expert_urls[expert]}/predict"
    try:
        # The outer timeout is the real budget: a slow expert is cut off, not waited for.
        async with asyncio.timeout(budget_ms / 1000):
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    content=context.model_dump_json(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Deadline-Ms": str(budget_ms),
                    },
                )
    except TimeoutError:
        return _not_ready(expert, "timeout", started)
    except httpx.HTTPError:
        return _not_ready(expert, "unreachable", started)
    if response.status_code != 200:
        return _not_ready(expert, f"http_{response.status_code}", started)
    try:
        risks = _RISKS.validate_json(response.content)
    except ValidationError:
        logger.warning("expert returned an invalid body", extra={"expert": expert.value})
        return _not_ready(expert, "invalid_response", started)
    if any(r.expert != expert or r.context_id != context.context_id for r in risks):
        return _not_ready(expert, "invalid_response", started)
    return ExpertResult(
        expert=expert,
        state="ready",
        risks=risks,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


async def fan_out_predict(
    settings: Settings, context: CommitContext, budget_ms: int
) -> list[ExpertResult]:
    """Call all four experts in parallel within one shared budget; result order is stable."""
    experts = list(Expert)
    results = await asyncio.gather(
        *(_predict_one(settings, e, context, budget_ms) for e in experts)
    )
    for r in results:
        logger.info(
            "expert predict",
            extra={"expert": r.expert.value, "state": r.state, "reason": r.reason},
        )
    return list(results)


async def forward_review(settings: Settings, expert: Expert, decision: ReviewDecision) -> bool:
    """POST the decision to the expert's /review; False if it could not be delivered."""
    url = f"{settings.expert_urls[expert]}/review"
    try:
        async with httpx.AsyncClient(timeout=settings.expert_call_timeout_s) as client:
            response = await client.post(url, content=decision.model_dump_json(), headers=_JSON)
    except httpx.HTTPError:
        logger.warning("review forward failed", extra={"expert": expert.value})
        return False
    return response.status_code in (200, 202, 204)


async def _health_one(settings: Settings, expert: Expert) -> tuple[str, ExpertHealth]:
    url = f"{settings.expert_urls[expert]}/health"
    try:
        async with httpx.AsyncClient(timeout=settings.expert_call_timeout_s) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return expert.value, ExpertHealth(status="unreachable")
        body = response.json()
        detail = body if isinstance(body, dict) else None
        return expert.value, ExpertHealth(status="ok", detail=detail)
    except (httpx.HTTPError, ValueError):
        return expert.value, ExpertHealth(status="unreachable")


async def probe_health(settings: Settings) -> dict[str, ExpertHealth]:
    """GET /health of every expert in parallel."""
    pairs = await asyncio.gather(*(_health_one(settings, e) for e in Expert))
    return dict(pairs)
