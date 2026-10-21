"""Gateway logic: cache, spending cap, provider call, validation with one retry, logging."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from .config import GatewayConfig
from .providers import Provider
from .stores import CacheStore, CallLog, CallRecord

logger = logging.getLogger(__name__)


class CompleteRequest(BaseModel):
    """Body of POST /complete."""

    expert: str
    prompt_version: str
    messages: list[dict[str, str]]
    json_schema: dict[str, Any]


class CompleteResponse(BaseModel):
    """Validated answer plus call metadata."""

    output: Any
    model: str
    prompt_version: str
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class GatewayError(Exception):
    """A refusal with an HTTP status and a clear JSON body."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("message", ""))
        self.status_code = status_code
        self.body = body


def cache_key(req: CompleteRequest, model: str) -> str:
    """Hash of the normalised request (trimmed message text, sorted keys, model, version)."""
    # DEFAULT: the expert is not part of the key, so identical requests share one cache entry.
    normalised = {
        "model": model,
        "prompt_version": req.prompt_version,
        "messages": [
            {"role": m.get("role", ""), "content": m.get("content", "").strip()}
            for m in req.messages
        ],
        "json_schema": req.json_schema,
    }
    blob = json.dumps(normalised, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse(text: str, validator: Draft202012Validator) -> tuple[Any, str | None]:
    """Return (value, None) when `text` is JSON valid for the schema, else (None, reason)."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc.msg}"
    errors = sorted(validator.iter_errors(value), key=lambda e: [str(p) for p in e.path])
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "(root)"
        return None, f"schema violation at {where}: {first.message[:200]}"
    return value, None


class Gateway:
    """The gateway; every collaborator is injected so tests run in memory."""

    def __init__(
        self,
        provider: Provider,
        config: GatewayConfig,
        cache: CacheStore,
        call_log: CallLog,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider = provider
        self.config = config
        self.cache = cache
        self.call_log = call_log
        self.clock = clock

    def complete(self, req: CompleteRequest) -> CompleteResponse:
        """Answer a request or raise GatewayError (400 bad request, 429 over cap, 502 invalid)."""
        cap = self.config.monthly_cap_usd.get(req.expert)
        if cap is None:
            raise GatewayError(
                400,
                {
                    "error": "unknown_expert",
                    "message": f"unknown expert {req.expert!r}",
                    "known": sorted(self.config.monthly_cap_usd),
                },
            )
        try:
            Draft202012Validator.check_schema(req.json_schema)
        except Exception as exc:
            raise GatewayError(
                400, {"error": "invalid_json_schema", "message": str(exc)[:300]}
            ) from exc
        validator = Draft202012Validator(req.json_schema)

        started = time.perf_counter()
        now = self.clock()
        model = self.config.default_model
        key = cache_key(req, model)

        # DEFAULT: cache hits are served even over the cap, because they cost nothing.
        cached = self.cache.get(key)
        if cached is not None:
            return self._finish(req, model, json.loads(cached), 0, 0, started, now, True)

        spent = self.call_log.month_spend_usd(req.expert, now)
        if spent >= cap:
            logger.warning(
                "llm cap reached",
                extra={"expert": req.expert, "spent": str(spent), "cap": str(cap)},
            )
            raise GatewayError(
                429,
                {
                    "error": "monthly_cap_exceeded",
                    "message": f"expert {req.expert!r} has used its monthly LLM budget",
                    "expert": req.expert,
                    "spent_usd": str(spent),
                    "cap_usd": str(cap),
                },
            )

        messages = list(req.messages)
        in_tokens = out_tokens = 0
        value: Any = None
        reason: str | None = "no attempt"
        for _attempt in range(2):  # the first answer plus ONE retry
            result = self.provider.complete(model, messages, req.json_schema)
            in_tokens += result.input_tokens
            out_tokens += result.output_tokens
            value, reason = _parse(result.text, validator)
            if reason is None:
                break
            messages = [
                *req.messages,
                {"role": "assistant", "content": result.text},
                {
                    "role": "user",
                    "content": f"Your answer was invalid ({reason}). "
                    "Reply again with only JSON that matches the schema.",
                },
            ]
        if reason is not None:
            self._log(req, model, in_tokens, out_tokens, started, now, False)
            raise GatewayError(
                502,
                {
                    "error": "invalid_model_output",
                    "message": "the model's answer failed schema validation after one retry",
                    "reason": reason,
                },
            )
        self.cache.set(key, json.dumps(value), self.config.cache_ttl_seconds)
        return self._finish(req, model, value, in_tokens, out_tokens, started, now, False)

    def _log(
        self,
        req: CompleteRequest,
        model: str,
        in_tokens: int,
        out_tokens: int,
        started: float,
        now: datetime,
        cache_hit: bool,
    ) -> Decimal:
        cost = Decimal(0) if cache_hit else self.config.cost_usd(model, in_tokens, out_tokens)
        self.call_log.record(
            CallRecord(
                expert=req.expert,
                model=model,
                prompt_version=req.prompt_version,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cost_usd=cost,
                cache_hit=cache_hit,
                latency_ms=int((time.perf_counter() - started) * 1000),
                created_at=now,
            )
        )
        logger.info(
            "llm call",
            extra={"expert": req.expert, "model": model, "cache_hit": cache_hit, "cost": str(cost)},
        )
        return cost

    def _finish(
        self,
        req: CompleteRequest,
        model: str,
        value: Any,
        in_tokens: int,
        out_tokens: int,
        started: float,
        now: datetime,
        cache_hit: bool,
    ) -> CompleteResponse:
        cost = self._log(req, model, in_tokens, out_tokens, started, now, cache_hit)
        return CompleteResponse(
            output=value,
            model=model,
            prompt_version=req.prompt_version,
            cache_hit=cache_hit,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=cost,
        )
