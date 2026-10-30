"""Provider interface, the mock provider and the DeepSeek provider (decision D53)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import ProviderRequestSettings
from .schema_gen import generate

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


@dataclass(frozen=True)
class ProviderResult:
    """Raw answer from a provider: text plus token counts."""

    text: str
    input_tokens: int
    output_tokens: int


class Provider(Protocol):
    """What the gateway needs from an LLM provider."""

    def complete(
        self, model: str, messages: list[dict[str, str]], json_schema: dict[str, Any]
    ) -> ProviderResult:
        """Answer the messages with text that should be JSON matching `json_schema`."""
        ...


def estimate_tokens(text: str) -> int:
    """Rough token count (DEFAULT: 4 characters per token; real providers report their own)."""
    return max(1, len(text) // 4)


class MockProvider:
    """Returns a schema-valid answer generated from the schema; never touches the network."""

    def complete(
        self, model: str, messages: list[dict[str, str]], json_schema: dict[str, Any]
    ) -> ProviderResult:
        """Generate a minimal valid JSON document for the schema."""
        text = json.dumps(generate(json_schema), sort_keys=True)
        prompt = "".join(m.get("content", "") for m in messages)
        return ProviderResult(text, estimate_tokens(prompt), estimate_tokens(text))


class ProviderError(Exception):
    """The provider gave no usable answer. `reason` is a short sentence that never holds the key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Reasons for the HTTP statuses DeepSeek documents. No response body is ever copied into them.
_STATUS_REASONS = {
    400: "the provider rejected the request (HTTP 400)",
    401: "the provider rejected the API key (HTTP 401); check LLM_API_KEY",
    402: "the provider account has insufficient balance (HTTP 402)",
    422: "the provider rejected the request parameters (HTTP 422)",
    429: "the provider is rate limiting requests (HTTP 429)",
}


def _status_reason(status: int) -> str:
    """Safe one-line reason for a non-200 provider status."""
    if status in _STATUS_REASONS:
        return _STATUS_REASONS[status]
    if status >= 500:
        return f"the provider is unavailable (HTTP {status})"
    return f"the provider answered with an unexpected status (HTTP {status})"


def _json_instruction(json_schema: dict[str, Any]) -> str:
    """System text for json_object mode, which takes no schema: show it in the prompt instead."""
    schema = json.dumps(json_schema, separators=(",", ":"), sort_keys=True)
    return (
        "Answer in json only: one json object, no other text, that matches this JSON Schema: "
        f"{schema}"
    )


class DeepSeekProvider:
    """DeepSeek chat completions (OpenAI-compatible) in JSON mode, thinking off."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEEPSEEK_BASE_URL,
        settings: ProviderRequestSettings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("LLM_API_KEY is empty: set it in .env to use LLM_PROVIDER=deepseek")
        self._api_key = api_key.strip()
        self._url = base_url.strip().rstrip("/") + "/chat/completions"
        self._settings = settings or ProviderRequestSettings()
        self._client = client

    def __repr__(self) -> str:
        return f"DeepSeekProvider(url={self._url!r})"  # never the key

    def complete(
        self, model: str, messages: list[dict[str, str]], json_schema: dict[str, Any]
    ) -> ProviderResult:
        """Ask DeepSeek; blank content is retried once, then raises ProviderError."""
        body: dict[str, Any] = {
            "model": model,
            "messages": [*messages, {"role": "system", "content": _json_instruction(json_schema)}],
            "response_format": {"type": "json_object"},
            # DEFAULT: thinking off (latency and cost); any reasoning_content is ignored anyway.
            "thinking": {"type": "disabled"},
            "max_tokens": self._settings.max_tokens,
            "stream": False,
        }
        in_tokens = out_tokens = 0
        for _attempt in range(2):  # the first call plus ONE retry, only for blank content
            text, used_in, used_out = self._post(body)
            in_tokens += used_in
            out_tokens += used_out
            if text.strip():
                return ProviderResult(text, in_tokens, out_tokens)
            logger.warning("provider returned blank content", extra={"model": model})
        raise ProviderError("the provider returned blank content twice")

    def _post(self, body: dict[str, Any]) -> tuple[str, int, int]:
        """One HTTP call; returns (content, prompt_tokens, completion_tokens)."""
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        timeout = self._settings.timeout_seconds
        try:
            if self._client is not None:
                response = self._client.post(self._url, json=body, headers=headers, timeout=timeout)
            else:
                response = httpx.post(self._url, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            raise ProviderError(f"the provider timed out after {timeout:g} s") from None
        except httpx.HTTPError as exc:
            raise ProviderError(f"the provider is unreachable ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ProviderError(_status_reason(response.status_code))
        try:
            data = response.json()
            content = data["choices"][0]["message"].get("content")
            usage = data.get("usage") or {}
            return (
                content if isinstance(content, str) else "",
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
            )
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError("the provider reply was not understood") from None


def get_provider(name: str, settings: ProviderRequestSettings | None = None) -> Provider:
    """Provider for LLM_PROVIDER: `mock` or `deepseek` (key from LLM_API_KEY)."""
    if name == "mock":
        return MockProvider()
    if name == "deepseek":
        return DeepSeekProvider(
            os.environ.get("LLM_API_KEY", ""),
            os.environ.get("LLM_BASE_URL", "").strip() or DEEPSEEK_BASE_URL,
            settings,
        )
    raise ValueError(f"LLM provider {name!r} is not implemented; use LLM_PROVIDER=mock or deepseek")
