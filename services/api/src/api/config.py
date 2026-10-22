"""Settings read from the environment (URLs, budgets, admin token). No secrets are logged."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from shared.contracts import Expert

# DEFAULT: local ports from docs/experts/README.md; compose overrides them with service names.
_DEFAULT_PORTS = {
    Expert.DEPENDENCY: 8101,
    Expert.BUILD: 8102,
    Expert.TEST: 8103,
    Expert.INFRA: 8104,
}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _default_expert_urls() -> dict[Expert, str]:
    urls: dict[Expert, str] = {}
    for expert, port in _DEFAULT_PORTS.items():
        env = os.environ.get(f"EXPERT_{expert.name}_URL", "").strip()
        urls[expert] = (env or f"http://127.0.0.1:{port}").rstrip("/")
    return urls


@dataclass(frozen=True)
class Settings:
    """Everything the API needs from its environment."""

    expert_urls: dict[Expert, str] = field(default_factory=_default_expert_urls)
    # Total budget for a pre-push /predict (ARCHITECTURE 3.1) and for PR notes (3.2).
    pre_push_budget_ms: int = field(
        default_factory=lambda: _int_env("PREDICT_BUDGET_PRE_PUSH_MS", 10_000)
    )
    pr_budget_ms: int = field(default_factory=lambda: _int_env("PREDICT_BUDGET_PR_MS", 120_000))
    # DEFAULT: 2 s for /health probes and /review forwarding, so a dead expert cannot hang them.
    expert_call_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("EXPERT_CALL_TIMEOUT_SECONDS", "") or 2.0)
    )
    # DEFAULT: key management is protected by one admin token until GitHub sign-in exists
    # (P3.1). Unset means /api-keys is disabled (503), never open.
    admin_token: str = field(default_factory=lambda: os.environ.get("API_ADMIN_TOKEN", "").strip())
