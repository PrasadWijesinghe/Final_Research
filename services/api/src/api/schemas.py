"""Request and response bodies that belong to the API itself (not cross-service contracts)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts import Diagnosis, Expert, ReviewDecision, RiskOutput, RoutedFailure


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpertResult(_Body):
    """One expert's answer to /predict, or the reason there is none."""

    expert: Expert
    state: Literal["ready", "not_ready"]
    # Why it is not ready: "timeout", "unreachable", "http_<status>" or "invalid_response".
    reason: str | None = None
    risks: list[RiskOutput] = Field(default_factory=list)
    latency_ms: int = Field(ge=0)


class PredictResponse(_Body):
    """Body of POST /predict: all four experts, ready or not."""

    context_id: UUID
    head_sha: str
    budget_ms: int
    elapsed_ms: int
    results: list[ExpertResult]


class ApiKeyRequest(_Body):
    """Body of POST /api-keys."""

    user_hash: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )  # DEFAULT: 64-hex (SHA-256) salted hash of the GitHub login


class ApiKeyIssued(_Body):
    """The plain key is in this response only; it cannot be shown again."""

    id: UUID
    key: str
    created_at: datetime


class CommitRisks(_Body):
    """Body of GET /commits/{sha}/risks."""

    head_sha: str
    risks: list[RiskOutput]


class RunItem(_Body):
    """A pre-run context with its predictions and, when known, how the run ended."""

    context_id: UUID
    repo: str
    head_sha: str
    event: str
    history_cutoff: datetime
    predictions: list[RiskOutput]
    run_id: int | None = None
    run_status: str | None = None
    run_conclusion: str | None = None  # None until the run has finished


class FailureView(_Body):
    """Body of GET /failures/{id}."""

    failure: RoutedFailure
    diagnoses: list[Diagnosis]
    reviews: list[ReviewDecision]


class ReviewAck(_Body):
    """Body of the POST /reviews answer."""

    decision_id: UUID
    forwarded: bool  # False when the expert could not be reached (the decision is still saved)


class ExpertHealth(_Body):
    """One expert's line in GET /health."""

    status: Literal["ok", "unreachable"]
    detail: dict[str, object] | None = None


class HealthResponse(_Body):
    """Body of GET /health."""

    status: Literal["ok", "degraded"]
    experts: dict[str, ExpertHealth]
