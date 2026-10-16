"""Developer review and knowledge-base write-back.

ReviewDecision: produced by the review UI, read by the expert that made the diagnosis.
VerifiedCase:   produced by an expert, stored in that expert's own vector collection.

All four proposals agree a case enters a knowledge base only after a developer
confirms the cause AND a later pipeline run passes. VerifiedCase enforces both.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .common import CommitSha, ContractModel, Expert, PersonHash, RepoRef, utc_now
from .diagnosis import Remediation


class ReviewDecision(ContractModel):
    decision_id: UUID = Field(default_factory=uuid4)
    diagnosis_id: UUID
    decision: Literal["accept", "reject", "modify", "request_revision"]
    reviewer_hash: PersonHash
    comment: str | None = Field(default=None, max_length=2_000)
    modified_remediation: Remediation | None = None  # required for "modify"
    cause_confirmed: bool  # does the reviewer agree with the root cause?
    seconds_to_decide: int | None = Field(default=None, ge=0)  # for time-to-diagnosis
    decided_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check(self) -> "ReviewDecision":
        if self.decision == "modify" and self.modified_remediation is None:
            raise ValueError("'modify' needs modified_remediation")
        if self.decision != "modify" and self.modified_remediation is not None:
            raise ValueError("modified_remediation is only for 'modify'")
        if self.decision == "reject" and self.cause_confirmed:
            raise ValueError("a rejected diagnosis cannot have a confirmed cause")
        return self


class VerifiedCase(ContractModel):
    case_id: str = Field(default_factory=lambda: str(uuid4()))
    expert: Expert
    origin: Literal["live", "mined"]  # live = from the platform; mined = from repo history
    failure_id: UUID | None = None  # set for live cases
    decision_id: UUID | None = None  # set for live cases
    repo: RepoRef
    repo_licence: str  # SPDX id, e.g. "MIT". Keep non-permissive repos out of a commercial KB.
    failure_summary: str = Field(min_length=1, max_length=1_000)
    log_signature: str = Field(min_length=1, max_length=2_000)  # key lines, sanitised
    root_cause: str = Field(min_length=1, max_length=1_000)
    fix_summary: str = Field(min_length=1, max_length=1_000)
    fix_commit_sha: CommitSha
    verifying_run_id: int  # the later run that passed
    verifying_run_passed: Literal[True]  # a case cannot be stored without a passing run
    tags: list[str] = Field(default_factory=list)  # e.g. FlakyFix category for test cases
    verified_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check(self) -> "VerifiedCase":
        if self.origin == "live" and (self.failure_id is None or self.decision_id is None):
            raise ValueError("live cases need failure_id and decision_id")
        return self
