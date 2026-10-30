"""DeepSeek provider: request shape, errors, key hygiene and model defaults. No network (respx)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from llm_gateway.api import create_app
from llm_gateway.config import ProviderRequestSettings, load_config, resolve_model
from llm_gateway.providers import (
    DeepSeekProvider,
    MockProvider,
    ProviderError,
    get_provider,
)
from llm_gateway.service import CompleteRequest, Gateway, GatewayError
from llm_gateway.stores import InMemoryCache, InMemoryCallLog

# A made-up value, only used to prove it never leaks into errors, logs or repr.
FAKE_KEY = "sk-test-not-a-real-key-0123456789"
URL = "https://api.deepseek.com/chat/completions"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
}


def reply(content: str | None, prompt: int = 120, completion: int = 30) -> httpx.Response:
    """A DeepSeek chat completion with `content`."""
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": content, "reasoning_content": "ignored thoughts"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
    )


def provider() -> DeepSeekProvider:
    return DeepSeekProvider(FAKE_KEY)


MESSAGES = [{"role": "user", "content": "why did it fail?"}]


def test_request_shape_and_token_counts() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(URL).mock(return_value=reply('{"verdict": "flaky"}', 120, 30))
        result = provider().complete("deepseek-flash", MESSAGES, SCHEMA)
    assert result.text == '{"verdict": "flaky"}'
    assert (result.input_tokens, result.output_tokens) == (120, 30)
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 2000
    assert body["messages"][0] == MESSAGES[0]
    last = body["messages"][-1]
    assert last["role"] == "system"
    assert "json" in last["content"].lower()
    assert '{"properties":{"verdict":{"type":"string"}}' in last["content"]  # compact schema
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"


def test_uses_configured_timeout_and_max_tokens() -> None:
    settings = ProviderRequestSettings(timeout_seconds=7, max_tokens=500)
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=reply("{}"))
        DeepSeekProvider(FAKE_KEY, settings=settings).complete("m", MESSAGES, SCHEMA)
    assert json.loads(route.calls.last.request.content)["max_tokens"] == 500
    assert route.calls.last.request.extensions["timeout"]["read"] == 7


def test_base_url_override_is_used() -> None:
    with respx.mock() as mock:
        route = mock.post("https://proxy.example/chat/completions").mock(return_value=reply("{}"))
        DeepSeekProvider(FAKE_KEY, "https://proxy.example/").complete("m", MESSAGES, SCHEMA)
    assert route.called


def test_blank_content_is_retried_once_and_tokens_add_up() -> None:
    with respx.mock() as mock:
        route = mock.post(URL).mock(
            side_effect=[reply("   ", 100, 5), reply('{"verdict":"x"}', 100, 20)]
        )
        result = provider().complete("deepseek-flash", MESSAGES, SCHEMA)
    assert route.call_count == 2
    assert result.text == '{"verdict":"x"}'
    assert (result.input_tokens, result.output_tokens) == (200, 25)


@pytest.mark.parametrize("content", ["  ", "", None])
def test_blank_content_twice_raises(content: str | None) -> None:
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=reply(content))
        with pytest.raises(ProviderError, match="blank content twice"):
            provider().complete("deepseek-flash", MESSAGES, SCHEMA)
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "API key"),
        (402, "insufficient balance"),
        (429, "rate limiting"),
        (500, "unavailable (HTTP 500)"),
        (503, "unavailable (HTTP 503)"),
    ],
)
def test_http_errors_map_to_clear_reasons_without_the_key(status: int, expected: str) -> None:
    with respx.mock() as mock:
        route = mock.post(URL).mock(
            return_value=httpx.Response(status, text=f"echo {FAKE_KEY} in the body")
        )
        with pytest.raises(ProviderError, match=re.escape(expected)) as info:
            provider().complete("deepseek-flash", MESSAGES, SCHEMA)
    assert route.call_count == 1  # no retry on HTTP errors
    assert FAKE_KEY not in info.value.reason
    assert FAKE_KEY not in repr(info.value)


def test_timeout_and_network_errors() -> None:
    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(ProviderError, match="timed out after 15 s"):
            provider().complete("m", MESSAGES, SCHEMA)
    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(ProviderError, match="unreachable"):
            provider().complete("m", MESSAGES, SCHEMA)


def test_malformed_reply_raises() -> None:
    with respx.mock() as mock:
        mock.post(URL).mock(return_value=httpx.Response(200, json={"choices": []}))
        with pytest.raises(ProviderError, match="not understood"):
            provider().complete("m", MESSAGES, SCHEMA)


def test_key_is_not_in_repr() -> None:
    assert FAKE_KEY not in repr(provider())


# --- get_provider -----------------------------------------------------------------------------


def test_get_provider_selects_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert isinstance(get_provider("mock"), MockProvider)
    assert isinstance(get_provider("deepseek"), DeepSeekProvider)
    with pytest.raises(ValueError, match="not implemented"):
        get_provider("openai")


@pytest.mark.parametrize("value", ["", "   "])
def test_missing_key_error_has_no_key(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LLM_API_KEY", value)
    with pytest.raises(ValueError, match="LLM_API_KEY") as info:
        get_provider("deepseek")
    assert FAKE_KEY not in str(info.value)
    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        get_provider("deepseek")


def test_get_provider_reads_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://proxy.example")
    with respx.mock() as mock:
        route = mock.post("https://proxy.example/chat/completions").mock(return_value=reply("{}"))
        get_provider("deepseek").complete("m", MESSAGES, SCHEMA)
    assert route.called


# --- model default per provider ---------------------------------------------------------------


def test_default_model_follows_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    config = load_config()
    assert resolve_model(config, "mock") == "mock-1"
    assert resolve_model(config, "deepseek") == "deepseek-flash"
    assert resolve_model(config, "something-else") == config.default_model == "mock-1"


def test_llm_model_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    assert resolve_model(load_config(), "deepseek") == "deepseek-v4-pro"
    monkeypatch.setenv("LLM_MODEL", "not-priced")
    with pytest.raises(ValueError, match="no price"):
        resolve_model(load_config(), "deepseek")


def test_deepseek_prices_are_in_config() -> None:
    config = load_config()
    # 1,000 input + 1,000 output tokens at peak prices per 1M: 0.30/1.20 and 1.32/3.96.
    assert float(config.cost_usd("deepseek-flash", 1_000_000, 1_000_000)) == pytest.approx(1.5)
    assert float(config.cost_usd("deepseek-v4-pro", 1_000_000, 1_000_000)) == pytest.approx(5.28)
    assert config.provider_request.timeout_seconds == 15
    assert config.monthly_cap_usd["test"] == 25


# --- through the gateway ----------------------------------------------------------------------


def make_gateway() -> tuple[Gateway, InMemoryCallLog]:
    log = InMemoryCallLog()
    gw = Gateway(provider(), load_config(), InMemoryCache(), log, model="deepseek-flash")
    return gw, log


def request() -> CompleteRequest:
    return CompleteRequest(
        expert="test", prompt_version="v1", messages=MESSAGES, json_schema=SCHEMA
    )


def test_gateway_uses_the_real_model_name_tokens_and_cost() -> None:
    gw, log = make_gateway()
    with respx.mock() as mock:
        mock.post(URL).mock(return_value=reply('{"verdict": "flaky"}', 2000, 500))
        res = gw.complete(request())
    assert res.model == "deepseek-flash"
    assert res.output == {"verdict": "flaky"}
    assert (res.input_tokens, res.output_tokens) == (2000, 500)
    assert float(res.cost_usd) == pytest.approx(2000 * 0.0003 / 1000 + 500 * 0.0012 / 1000)
    assert len(log.calls) == 1


def test_gateway_invalid_json_goes_through_the_normal_retry_path() -> None:
    gw, _ = make_gateway()
    with respx.mock() as mock:
        route = mock.post(URL).mock(
            side_effect=[reply("not json at all"), reply('{"verdict": "ok"}')]
        )
        res = gw.complete(request())
    assert res.output == {"verdict": "ok"}
    assert route.call_count == 2


def test_gateway_maps_provider_failure_to_502_provider_error(caplog: Any) -> None:
    gw, log = make_gateway()
    with respx.mock() as mock, caplog.at_level("DEBUG"):
        mock.post(URL).mock(return_value=httpx.Response(402))
        with pytest.raises(GatewayError) as info:
            gw.complete(request())
    assert info.value.status_code == 502
    assert info.value.body["error"] == "provider_error"
    assert "insufficient balance" in info.value.body["reason"]
    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in json.dumps(info.value.body)
    assert log.calls == []  # nothing paid, nothing logged


def test_api_returns_502_so_the_expert_falls_back() -> None:
    gw, _ = make_gateway()
    client = TestClient(create_app(gw))
    with respx.mock(assert_all_called=False) as mock:
        mock.post(URL).mock(return_value=httpx.Response(401))
        res = client.post("/complete", json=request().model_dump())
    assert res.status_code == 502
    assert res.json()["error"] == "provider_error"
    assert FAKE_KEY not in res.text
