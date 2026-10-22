"""Diagnosis: produced by an expert, read by the review UI.

The common part (root cause, evidence, retrieved cases, remediation, confidence) is the
same for every expert, as all four proposals require. `details` holds each expert's own
fields. The detail models below are DRAFTS taken from each proposal: each member
should confirm or edit their own.

Rules enforced here:
  - every diagnosis cites at least one evidence line
  - a remediation may only cite cases that were actually retrieved
  - an expert can say "not mine" (misrouted) instead of forcing a diagnosis
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .common import ContractModel, Expert, FailureCategory, Part, Probability, utc_now


class EvidenceLine(Part):
    source: Literal["log", "stack_trace", "diff", "config"]
    text: str = Field(max_length=1_000)  # sanitised
    line: int | None = Field(default=None, ge=1)  # log line or file line
    path: str | None = None  # file path for diff/config/stack frames


class RetrievedCase(Part):
    case_id: str
    similarity: float = Field(ge=0.0, le=1.0)
    summary: str
    fix_summary: str
    source_url: str | None = None  # link to the fixing commit or PR


class Remediation(Part):
    summary: str = Field(min_length=1, max_length=500)
    steps: list[str] = Field(default_factory=list)
    # A unified diff the developer may apply by hand. Never applied automatically.
    suggested_patch: str | None = None
    cites_case_ids: list[str] = Field(default_factory=list)


# ---- Per-expert details (drafts; each owner confirms their own) -----------------


class DependencyDetails(Part):
    expert: Literal["dependency"] = "dependency"
    packages: list[str] = Field(default_factory=list)  # packages implicated
    kind: Literal[
        "version_conflict", "lockfile_mismatch", "node_incompatibility", "missing_package", "other"
    ] = "other"


class BuildLocation(Part):
    path: str
    line: int | None = Field(default=None, ge=1)
    code: str | None = None  # e.g. "TS2345" or an ESLint rule id


class BuildDetails(Part):
    expert: Literal["build"] = "build"
    tool: Literal["tsc", "eslint", "bundler", "other"]
    locations: list[BuildLocation] = Field(default_factory=list)


class BehaviourDetails(Part):
    """Test & Application Behaviour expert. (Not named TestDetails: pytest would collect it.)"""

    expert: Literal["test"] = "test"
    verdict: Literal["genuine", "flaky", "unclear"]
    verdict_confidence: Probability
    framework: Literal["jest", "mocha", "vitest", "other"]
    failing_tests: list[str] = Field(min_length=1)  # full test names
    stack_touches_changed_code: bool  # did the stack trace pass through changed lines?
    pre_run_flaky_risk: Probability | None = None  # from this commit's RiskOutput
    suggest_rerun: bool  # True only for a confident flaky verdict

    @model_validator(mode="after")
    def _rerun_only_if_flaky(self) -> "BehaviourDetails":
        if self.suggest_rerun and self.verdict != "flaky":
            raise ValueError("suggest_rerun is only allowed for a flaky verdict")
        return self


class InfraDetails(Part):
    expert: Literal["infra"] = "infra"
    area: Literal[
        "workflow_config", "runner", "permissions", "container", "network", "deployment", "other"
    ]
    sub_pattern: str | None = None  # finer pattern, once Member 4 defines them


ExpertDetails = Annotated[
    DependencyDetails | BuildDetails | BehaviourDetails | InfraDetails,
    Field(discriminator="expert"),
]

# ---------------------------------------------------------------------------------


class Diagnosis(ContractModel):
    diagnosis_id: UUID = Field(default_factory=uuid4)
    failure_id: UUID  # the RoutedFailure this answers
    expert: Expert
    status: Literal["diagnosed", "misrouted", "error"]
    error_message: str | None = None

    # Filled when status="misrouted": the expert thinks this belongs elsewhere.
    suggested_category: FailureCategory | None = None

    root_cause: str | None = Field(default=None, max_length=1_000)
    confidence: Probability | None = None
    evidence: list[EvidenceLine] = Field(default_factory=list)
    retrieved_cases: list[RetrievedCase] = Field(default_factory=list)
    remediation: Remediation | None = None
    details: ExpertDetails | None = None

    llm_model: str | None = None
    prompt_version: str | None = None  # git hash or tag of the prompt file
    latency_ms: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check(self) -> "Diagnosis":
        if self.status == "error":
            if self.error_message is None:
                raise ValueError("status='error' needs error_message")
            return self
        if self.status == "misrouted":
            if self.suggested_category is None:
                raise ValueError("status='misrouted' needs suggested_category")
            return self
        # status == "diagnosed"
        missing = [
            name
            for name in ("root_cause", "confidence", "remediation", "details")
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(f"a diagnosis needs: {', '.join(missing)}")
        if not self.evidence:
            raise ValueError("a diagnosis must cite at least one evidence line")
        if self.details.expert != self.expert.value:
            raise ValueError("details must belong to the same expert")
        retrieved = {c.case_id for c in self.retrieved_cases}
        unknown = set(self.remediation.cites_case_ids) - retrieved
        if unknown:
            raise ValueError(f"remediation cites cases that were not retrieved: {sorted(unknown)}")
        return self
