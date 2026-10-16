"""Risk output: produced by each expert, read by the dashboard and the PR note.

Scope differs by expert (from the proposals):
  dependency, build, infra: one prediction per commit  -> scope="commit"
  test:                     one prediction per test job -> scope="job" with job_key set

The four probabilities predict different, non-exclusive events, so they may sum to
more than 1. The dashboard must never normalise them against each other.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .common import ContractModel, Expert, Part, Probability, utc_now


class RiskFactor(Part):
    """One SHAP-ranked feature, phrased as a risk factor, never as a cause."""

    feature: str  # model feature name, e.g. "unawaited_promises_in_changed_tests"
    value: str | float | int | bool  # the feature's value for this commit
    shap_value: float
    direction: Literal["raises", "lowers"]
    reason: str = Field(min_length=1, max_length=200)  # plain language for developers
    path: str | None = None  # file the factor points at, if any
    line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _direction_matches_sign(self) -> "RiskFactor":
        if (self.shap_value >= 0) != (self.direction == "raises"):
            raise ValueError("direction must match the sign of shap_value")
        return self


class RiskOutput(ContractModel):
    prediction_id: UUID = Field(default_factory=uuid4)
    context_id: UUID  # the CommitContext this prediction is for
    expert: Expert
    model_version: str  # e.g. MLflow run id or "lgbm-2026-11-03"
    scope: Literal["commit", "job"]
    workflow_path: str | None = None  # required when scope="job"
    job_key: str | None = None  # required when scope="job"

    status: Literal["ok", "degraded", "error"] = "ok"  # degraded = ran with missing inputs
    error_message: str | None = None

    probability: Probability | None = None  # None only when status="error"
    threshold: Probability  # operating threshold chosen on validation data
    alert: bool  # True when probability >= threshold; the UI shows only alerts
    # Expert-specific breakdown, e.g. test: {"genuine_fail": 0.12, "flaky_fail": 0.31}
    # build: {"tsc": .., "eslint": .., "bundler": ..}; infra: per sub-pattern.
    sub_risks: dict[str, Probability] = Field(default_factory=dict)
    top_factors: list[RiskFactor] = Field(default_factory=list, max_length=5)

    latency_ms: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check(self) -> "RiskOutput":
        if self.scope == "job" and not (self.workflow_path and self.job_key):
            raise ValueError("scope='job' needs workflow_path and job_key")
        if self.status == "error":
            if self.error_message is None:
                raise ValueError("status='error' needs error_message")
            if self.alert:
                raise ValueError("an errored prediction cannot raise an alert")
            return self
        if self.probability is None:
            raise ValueError("probability is required unless status='error'")
        if self.alert != (self.probability >= self.threshold):
            raise ValueError("alert must equal probability >= threshold")
        if self.alert and not self.top_factors:
            raise ValueError("an alert must carry at least one reason (top_factors)")
        return self
