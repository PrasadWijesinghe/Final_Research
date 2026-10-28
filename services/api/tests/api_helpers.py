"""Shared test helpers: fake expert routes, payload fixture and contract builders."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import respx
from api.config import Settings

from shared.contracts import (
    Diagnosis,
    Expert,
    RepoRef,
    RiskOutput,
    RoutedFailure,
)
from shared.contracts.routed_failure import FailedStep

FIXTURES = Path(__file__).parent / "fixtures"
URLS = {e: f"http://expert-{e.value}.test" for e in Expert}
# Obviously fake material for tests.
USER_HASH = "0" * 64
ADMIN_TOKEN = "fake-admin-token-for-tests"


def payload_json() -> dict[str, Any]:
    """The fixture pre-push payload as a dict."""
    data: dict[str, Any] = json.loads((FIXTURES / "pre_push_payload.json").read_text("utf-8"))
    return data


def settings(**kw: Any) -> Settings:
    """Settings pointing at the fake expert hosts."""
    base: dict[str, Any] = {"expert_urls": dict(URLS), "admin_token": ADMIN_TOKEN}
    return Settings(**{**base, **kw})


def risk_for(expert: Expert, context: dict[str, Any]) -> dict[str, Any]:
    """A contract-valid RiskOutput (as JSON) for the request's context."""
    out = RiskOutput(
        context_id=context["context_id"],
        expert=expert,
        model_version="stub",
        scope="commit",
        probability=0.1,
        threshold=0.5,
        alert=False,
        latency_ms=3,
    )
    dumped: dict[str, Any] = out.model_dump(mode="json")
    return dumped


Handler = Callable[[httpx.Request], httpx.Response]


def mock_experts(
    router: respx.MockRouter,
    *,
    slow: dict[Expert, float] | None = None,
    overrides: dict[Expert, Handler] | None = None,
    seen: list[httpx.Request] | None = None,
) -> None:
    """Route POST /predict of each expert; `slow` maps an expert to seconds of delay."""
    slow = slow or {}
    overrides = overrides or {}
    for expert in Expert:

        async def handler(request: httpx.Request, expert: Expert = expert) -> httpx.Response:
            if seen is not None:
                seen.append(request)
            if expert in overrides:
                return overrides[expert](request)
            if expert in slow:
                await asyncio.sleep(slow[expert])
            context = json.loads(request.content)
            return httpx.Response(200, json=[risk_for(expert, context)])

        router.post(f"{URLS[expert]}/predict").mock(side_effect=handler)


def routed_failure(**kw: Any) -> RoutedFailure:
    """A contract-valid RoutedFailure."""
    base: dict[str, Any] = {
        "repo": RepoRef(owner="acme", name="widgets"),
        "head_sha": "a" * 40,
        "run_id": 1,
        "run_attempt": 1,
        "job_id": 2,
        "job_name": "test",
        "workflow_path": ".github/workflows/ci.yml",
        "failed_steps": [FailedStep(number=3, name="npm test", conclusion="failure")],
        "log_ref": "logs/acme/widgets/1/2.log.gz",
        "log_excerpt": "FAIL test/index.test.ts",
        "predicted_category": "test",
        "routing_confidence": 0.9,
        "sent_to": [Expert.TEST],
    }
    return RoutedFailure(**{**base, **kw})


def error_diagnosis(failure_id: Any, expert: Expert = Expert.TEST) -> Diagnosis:
    """A minimal contract-valid Diagnosis (status error)."""
    return Diagnosis(
        failure_id=failure_id,
        expert=expert,
        status="error",
        error_message="stub",
        latency_ms=5,
    )
