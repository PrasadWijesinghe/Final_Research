"""Client for the LLM gateway (P4.11): one call, a hard timeout, no retries, no secrets logged.

The gateway URL comes from `LLM_GATEWAY_URL`. When it is unset, no call is made and the caller
uses the deterministic template. Every failure becomes `GatewayUnavailable` with a short reason
that is safe to show (never the prompt, never the reply body).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

URL_ENV = "LLM_GATEWAY_URL"
EXPERT_NAME = "test"  # the gateway's name for this expert (its cap and usage are per expert)


class GatewayUnavailable(Exception):
    """The gateway gave no usable answer. `reason` is a short, safe sentence."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GatewayReply:
    """A validated JSON answer and the model name the gateway reported."""

    output: dict[str, Any]
    model: str
    cache_hit: bool


def gateway_url() -> str | None:
    """`LLM_GATEWAY_URL` without a trailing slash, or None when it is unset or empty."""
    return os.environ.get(URL_ENV, "").strip().rstrip("/") or None


def _error_code(response: httpx.Response) -> str:
    """The gateway's `error` code from a refusal body, or an empty string."""
    try:
        return str(response.json().get("error", ""))
    except (ValueError, AttributeError):
        return ""


def complete(
    messages: list[dict[str, str]],
    json_schema: dict[str, Any],
    prompt_version: str,
    timeout_seconds: float,
) -> GatewayReply:
    """POST /complete; raise GatewayUnavailable on any failure."""
    url = gateway_url()
    if url is None:
        raise GatewayUnavailable(f"{URL_ENV} is not set")
    body = {
        "expert": EXPERT_NAME,
        "prompt_version": prompt_version,
        "messages": messages,
        "json_schema": json_schema,
    }
    try:
        response = httpx.post(f"{url}/complete", json=body, timeout=timeout_seconds)
    except httpx.TimeoutException as exc:
        raise GatewayUnavailable(f"gateway timed out after {timeout_seconds:g} s") from exc
    except httpx.HTTPError as exc:
        raise GatewayUnavailable(f"gateway unreachable ({type(exc).__name__})") from exc
    if response.status_code == 429:
        raise GatewayUnavailable("monthly LLM spending cap reached (HTTP 429)")
    if response.status_code == 502 and _error_code(response) == "provider_error":
        raise GatewayUnavailable("the model provider failed (HTTP 502)")
    if response.status_code == 502:
        raise GatewayUnavailable("the model returned an invalid answer twice (HTTP 502)")
    if response.status_code != 200:
        raise GatewayUnavailable(f"gateway refused the request (HTTP {response.status_code})")
    try:
        data = response.json()
        output, model = data["output"], str(data["model"])
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayUnavailable("gateway reply was not understood") from exc
    if not isinstance(output, dict):
        raise GatewayUnavailable("gateway reply was not a JSON object")
    return GatewayReply(output=output, model=model, cache_hit=bool(data.get("cache_hit")))
