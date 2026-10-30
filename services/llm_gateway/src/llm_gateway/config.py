"""Gateway settings from `config/llm.yaml` (env: LLM_CONFIG_FILE, LLM_PROVIDER, LLM_MODEL)."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# DEFAULT: config lives in <checkout>/config/llm.yaml, like the other YAML configs.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "llm.yaml"


class ModelPrice(BaseModel):
    """Price of one model in USD per 1,000 tokens."""

    input_per_1k_usd: Decimal
    output_per_1k_usd: Decimal


class ProviderRequestSettings(BaseModel):
    """How a real provider is called (the `provider_request` block of `config/llm.yaml`)."""

    # DEFAULT: 15 s, under the Test expert's 20 s diagnosis timeout so the gateway fails first.
    timeout_seconds: float = Field(default=15.0, gt=0)
    # DEFAULT: 2000 output tokens; json_object mode needs a cap so the JSON is not cut off.
    max_tokens: int = Field(default=2000, gt=0)


class GatewayConfig(BaseModel):
    """Parsed `config/llm.yaml`."""

    default_model: str
    provider_models: dict[str, str] = {}
    provider_request: ProviderRequestSettings = ProviderRequestSettings()
    models: dict[str, ModelPrice]
    monthly_cap_usd: dict[str, Decimal]
    cache_ttl_seconds: int = Field(gt=0)

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        """Cost of a call from the model's per-1k-token prices."""
        price = self.models[model]
        return (
            price.input_per_1k_usd * input_tokens + price.output_per_1k_usd * output_tokens
        ) / Decimal(1000)


def load_config(path: Path | None = None) -> GatewayConfig:
    """Load the gateway config from `path`, LLM_CONFIG_FILE or the default location."""
    chosen = path or Path(os.environ.get("LLM_CONFIG_FILE") or DEFAULT_CONFIG_PATH)
    return GatewayConfig.model_validate(yaml.safe_load(chosen.read_text("utf-8")))


def provider_name() -> str:
    """The provider selected by LLM_PROVIDER (DEFAULT: mock, the safe choice)."""
    return os.environ.get("LLM_PROVIDER", "").strip() or "mock"


def resolve_model(config: GatewayConfig, provider: str) -> str:
    """Model for `provider`: LLM_MODEL, else `provider_models[provider]`, else `default_model`."""
    model = (
        os.environ.get("LLM_MODEL", "").strip()
        or config.provider_models.get(provider)
        or config.default_model
    )
    if model not in config.models:
        raise ValueError(f"model {model!r} has no price in config/llm.yaml `models`")
    return model
