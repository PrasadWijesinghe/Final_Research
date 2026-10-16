"""Routed failure: produced by services/router, read by the chosen expert(s).

When routing confidence is low, the router sends the same failure to the two most
likely experts (rank 1 and rank 2), as all four proposals specify. Each returns its
own Diagnosis and the developer weighs both.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .common import (
    CommitSha,
    ContractModel,
    Expert,
    FailureCategory,
    Part,
    Probability,
    RepoRef,
    StorageRef,
    utc_now,
)


class FailedStep(Part):
    number: int = Field(ge=1)
    name: str
    conclusion: Literal["failure", "cancelled", "timed_out"]


class RoutedFailure(ContractModel):
    failure_id: UUID = Field(default_factory=uuid4)
    context_id: UUID | None = None  # None when no pre-run context exists (e.g. old runs)
    repo: RepoRef
    head_sha: CommitSha
    run_id: int
    run_attempt: int = Field(ge=1)  # attempt > 1 means a re-run
    job_id: int
    job_name: str
    workflow_path: str
    job_key: str | None = None  # matched to WorkflowJob.job_key when possible
    failed_steps: list[FailedStep] = Field(min_length=1)

    log_ref: StorageRef  # full sanitised log in shared storage
    log_excerpt: str = Field(max_length=20_000)  # sanitised tail of the failed step

    predicted_category: FailureCategory
    routing_confidence: Probability
    sent_to: list[Expert] = Field(min_length=1, max_length=2)  # rank order
    # True when produced by replay mode. Replay results are stored separately and must
    # never cause anything visible on GitHub (no PR notes, no checks).
    replay: bool = False
    routed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check(self) -> "RoutedFailure":
        if len(set(self.sent_to)) != len(self.sent_to):
            raise ValueError("sent_to must not repeat an expert")
        return self
