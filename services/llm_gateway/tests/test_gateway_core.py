"""Gateway behaviour with the mock provider and in-memory stores (no network, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from llm_gateway.api import create_app
from llm_gateway.config import load_config
from llm_gateway.providers import MockProvider, ProviderResult, get_provider
from llm_gateway.service import CompleteRequest, Gateway, GatewayError
from llm_gateway.stores import (
    CallRecord,
    InMemoryCache,
    InMemoryCallLog,
    RedisCache,
)

from shared.contracts import Diagnosis, RiskOutput

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["verdict", "score"],
}
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


class ScriptedProvider:
    """Returns canned texts in order; records how many calls it got."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[list[dict[str, str]]] = []

    def complete(
        self, model: str, messages: list[dict[str, str]], json_schema: dict[str, Any]
    ) -> ProviderResult:
        self.calls.append(messages)
        return ProviderResult(self.texts.pop(0), 100, 50)


def make_gateway(
    provider: Any = None, caps: dict[str, Decimal] | None = None
) -> tuple[Gateway, InMemoryCallLog, InMemoryCache]:
    config = load_config()
    if caps is not None:
        config = config.model_copy(update={"monthly_cap_usd": caps})
    log, cache = InMemoryCallLog(), InMemoryCache()
    gw = Gateway(provider or MockProvider(), config, cache, log, clock=lambda: NOW)
    return gw, log, cache


def request(prompt: str = "hello", schema: dict[str, Any] | None = None) -> CompleteRequest:
    return CompleteRequest(
        expert="test",
        prompt_version="v1",
        messages=[{"role": "user", "content": prompt}],
        json_schema=schema or SCHEMA,
    )


def test_mock_output_is_schema_valid_for_contract_schemas() -> None:
    for model in (Diagnosis, RiskOutput):
        schema = model.model_json_schema()
        value = get_provider("mock").complete("m", [], schema)
        import json

        Draft202012Validator(schema).validate(json.loads(value.text))


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        get_provider("openai")


def test_complete_returns_validated_output_and_logs() -> None:
    gw, log, _ = make_gateway()
    res = gw.complete(request())
    Draft202012Validator(SCHEMA).validate(res.output)
    assert res.cache_hit is False and res.cost_usd > 0
    (call,) = log.calls
    assert (call.expert, call.prompt_version, call.cache_hit) == ("test", "v1", False)
    assert call.input_tokens > 0 and call.output_tokens > 0 and call.latency_ms >= 0


def test_identical_request_hits_cache_and_is_free() -> None:
    provider = ScriptedProvider('{"verdict": "a", "score": 1}')
    gw, log, _ = make_gateway(provider)
    first = gw.complete(request("  hello  "))
    second = gw.complete(request("hello"))  # same after normalising whitespace
    assert second.cache_hit is True and second.output == first.output
    assert len(provider.calls) == 1
    assert [c.cache_hit for c in log.calls] == [False, True]
    assert log.calls[1].cost_usd == 0


def test_different_prompt_version_misses_cache() -> None:
    gw, log, _ = make_gateway()
    gw.complete(request())
    other = request().model_copy(update={"prompt_version": "v2"})
    assert gw.complete(other).cache_hit is False


def test_redis_cache_roundtrip_and_ttl() -> None:
    cache = RedisCache(fakeredis.FakeRedis())
    assert cache.get("k") is None
    cache.set("k", '{"a": 1}', 60)
    assert cache.get("k") == '{"a": 1}'


def test_invalid_output_retries_once_then_succeeds() -> None:
    provider = ScriptedProvider("not json", '{"verdict": "ok", "score": 2}')
    gw, log, _ = make_gateway(provider)
    res = gw.complete(request())
    assert res.output == {"verdict": "ok", "score": 2}
    assert len(provider.calls) == 2
    assert "invalid" in provider.calls[1][-1]["content"]
    assert len(log.calls) == 1 and log.calls[0].input_tokens == 200  # both attempts counted


def test_schema_violation_retries_once_then_fails_with_502() -> None:
    provider = ScriptedProvider('{"verdict": "ok"}', '{"verdict": 5, "score": 1}')
    gw, log, _ = make_gateway(provider)
    with pytest.raises(GatewayError) as err:
        gw.complete(request())
    assert err.value.status_code == 502
    assert err.value.body["error"] == "invalid_model_output"
    assert len(provider.calls) == 2  # exactly one retry
    assert len(log.calls) == 1  # failed calls still cost money, so they are logged


def test_over_cap_is_refused_and_provider_not_called() -> None:
    provider = ScriptedProvider('{"verdict": "a", "score": 1}')
    gw, log, _ = make_gateway(provider, caps={"test": Decimal("0.01")})
    log.record(CallRecord("test", "mock-1", "v0", 1, 1, Decimal("0.01"), False, 1, created_at=NOW))
    with pytest.raises(GatewayError) as err:
        gw.complete(request())
    assert err.value.status_code == 429
    assert err.value.body["error"] == "monthly_cap_exceeded"
    assert err.value.body["cap_usd"] == "0.01"
    assert provider.calls == []


def test_cap_is_per_expert_and_resets_each_month() -> None:
    gw, log, _ = make_gateway(caps={"test": Decimal("0.01"), "build": Decimal("0.01")})
    last_month = datetime(2026, 9, 30, 23, 59, tzinfo=UTC)
    log.record(CallRecord("test", "m", None, 1, 1, Decimal("5"), False, 1, created_at=last_month))
    log.record(CallRecord("build", "m", None, 1, 1, Decimal("5"), False, 1, created_at=NOW))
    assert gw.complete(request()).cache_hit is False  # last month's spend does not count
    other = request("another prompt").model_copy(update={"expert": "build"})
    with pytest.raises(GatewayError):
        gw.complete(other)


def test_cap_reached_through_normal_spending() -> None:
    gw, _, _ = make_gateway(caps={"test": Decimal("0.000001")})
    gw.complete(request("one"))  # first call is allowed (spend 0 < cap) and pushes spend over
    with pytest.raises(GatewayError) as err:
        gw.complete(request("two"))
    assert err.value.status_code == 429


def test_cache_hit_still_served_over_cap() -> None:
    gw, log, _ = make_gateway(caps={"test": Decimal("0.000001")})
    gw.complete(request())
    assert gw.complete(request()).cache_hit is True


def test_unknown_expert_and_bad_schema_are_400() -> None:
    gw, _, _ = make_gateway()
    with pytest.raises(GatewayError) as err:
        gw.complete(request().model_copy(update={"expert": "nobody"}))
    assert err.value.status_code == 400
    with pytest.raises(GatewayError) as err:
        gw.complete(request(schema={"type": "nonsense"}))
    assert err.value.body["error"] == "invalid_json_schema"


def test_http_endpoints() -> None:
    gw, log, _ = make_gateway(caps={"test": Decimal("0.000001")})
    client = TestClient(create_app(gw))
    assert client.get("/health").json()["status"] == "ok"
    body = request().model_dump()
    ok = client.post("/complete", json=body)
    assert ok.status_code == 200
    assert set(ok.json()) >= {"output", "model", "cache_hit", "cost_usd"}
    refused = client.post("/complete", json={**body, "prompt_version": "v9"})
    assert refused.status_code == 429
    assert refused.json()["error"] == "monthly_cap_exceeded"
    assert client.post("/complete", json={"expert": "test"}).status_code == 422


def test_api_key_is_never_logged(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    monkeypatch.setenv("LLM_API_KEY", "fake-key-do-not-log")
    gw, _, _ = make_gateway()
    with caplog.at_level("DEBUG"):
        gw.complete(request())
    assert "fake-key-do-not-log" not in caplog.text
