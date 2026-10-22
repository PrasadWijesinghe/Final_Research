"""Provider interface and the mock provider. No real provider yet (decision O5)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from .schema_gen import generate


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


def get_provider(name: str) -> Provider:
    """Provider for LLM_PROVIDER; only `mock` exists until the provider is chosen (O5)."""
    if name == "mock":
        return MockProvider()
    raise ValueError(f"LLM provider {name!r} is not implemented yet; use LLM_PROVIDER=mock")
