"""Failure label: the shared training label for one failed job attempt.

Why this is shared: every expert trains on the same runs. Build and Infra both use
"failures of other categories" as negatives, and Dependency calls for a common
taxonomy. So each failed job gets ONE primary category, agreed across the team,
and each expert reads the labels of all four categories.

Disagreements (two experts claim the same job) go to manual review, and the
final label is stored with labelled_by="manual".
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import ContractModel, FailureCategory, PersonHash, RepoRef, utc_now


class FailureLabel(ContractModel):
    repo: RepoRef
    run_id: int
    run_attempt: int = Field(ge=1)
    job_id: int
    category: FailureCategory
    # Expert-specific refinement, e.g. test: "flaky" | "genuine"; infra: a sub-pattern.
    sub_label: str | None = None
    labelled_by: Literal["rule", "manual", "injected"]
    rule_name: str | None = None  # which labelling rule fired, e.g. "rerun_pass_same_sha"
    labeller_hash: PersonHash | None = None  # who reviewed it, for manual labels
    confidence: Literal["high", "medium", "low"] = "high"
    notes: str | None = Field(default=None, max_length=1_000)
    labelled_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check(self) -> "FailureLabel":
        if self.labelled_by == "rule" and not self.rule_name:
            raise ValueError("rule labels need rule_name")
        if self.labelled_by == "manual" and self.labeller_hash is None:
            raise ValueError("manual labels need labeller_hash")
        return self
