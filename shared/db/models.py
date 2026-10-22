"""SQLAlchemy 2 models for the `raw` (collector) and `platform` (backend) schemas (DATA.md 4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# DEFAULT: every datetime column is timestamptz; all list columns are native Postgres arrays
# (text[] / bigint[]) rather than jsonb; raw GitHub payloads and contract payloads are jsonb.
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    """Declarative base shared by the raw and platform models."""


# ---------------------------------------------------------------- raw schema
# DEFAULT: primary keys below are not spelled out in DATA.md 4.1 beyond workflow_runs; each is the
# natural key GitHub gives us (job ids are unique across attempts, so jobs keys on job_id alone).


class Repo(Base):
    """A tracked GitHub repository."""

    __tablename__ = "repos"
    __table_args__ = {"schema": "raw"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    full_name: Mapped[str] = mapped_column(Text, unique=True)
    member: Mapped[str | None] = mapped_column(Text)
    licence: Mapped[str | None] = mapped_column(Text)
    default_branch: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class SyncState(Base):
    """Backfill and daily-sync progress for one repo."""

    __tablename__ = "sync_state"
    __table_args__ = {"schema": "raw"}

    repo_id: Mapped[int] = mapped_column(ForeignKey("raw.repos.id"), primary_key=True)
    backfill_done_at: Mapped[datetime | None] = mapped_column(TZ)
    backfill_cursor: Mapped[str | None] = mapped_column(Text)
    last_daily_sync_at: Mapped[datetime | None] = mapped_column(TZ)


class Workflow(Base):
    """A workflow file in a repo."""

    __tablename__ = "workflows"
    __table_args__ = {"schema": "raw"}

    repo_id: Mapped[int] = mapped_column(ForeignKey("raw.repos.id"), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    relevant: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    detected_at_sha: Mapped[str | None] = mapped_column(Text)


class WorkflowRun(Base):
    """One attempt of a workflow run."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_repo_created", "repo_id", "created_at"),
        Index("ix_workflow_runs_head_sha", "repo_id", "head_sha"),
        {"schema": "raw"},
    )

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    repo_id: Mapped[int] = mapped_column(ForeignKey("raw.repos.id"))
    workflow_path: Mapped[str | None] = mapped_column(Text)
    head_sha: Mapped[str] = mapped_column(Text)
    head_branch: Mapped[str | None] = mapped_column(Text)
    event: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TZ)
    run_started_at: Mapped[datetime | None] = mapped_column(TZ)
    updated_at: Mapped[datetime | None] = mapped_column(TZ)
    actor_hash: Mapped[str | None] = mapped_column(Text)
    pr_numbers: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default="{}"
    )
    source: Mapped[str] = mapped_column(Text, default="collector", server_default="collector")
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Job(Base):
    """One job of a workflow run attempt."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_run_attempt", "run_id", "attempt"), {"schema": "raw"})

    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    run_id: Mapped[int] = mapped_column(BigInteger)
    attempt: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(Text)
    job_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    runner_os: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Step(Base):
    """One step of a job."""

    __tablename__ = "steps"
    __table_args__ = {"schema": "raw"}

    job_id: Mapped[int] = mapped_column(ForeignKey("raw.jobs.job_id"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)


class Commit(Base):
    """A commit (message is sanitised before it is stored)."""

    __tablename__ = "commits"
    __table_args__ = {"schema": "raw"}

    repo_id: Mapped[int] = mapped_column(ForeignKey("raw.repos.id"), primary_key=True)
    sha: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_shas: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    author_hash: Mapped[str | None] = mapped_column(Text)
    committed_at: Mapped[datetime | None] = mapped_column(TZ)
    message: Mapped[str | None] = mapped_column(Text)


class CommitFile(Base):
    """One file changed by a commit."""

    __tablename__ = "commit_files"
    __table_args__ = (
        ForeignKeyConstraint(["repo_id", "sha"], ["raw.commits.repo_id", "raw.commits.sha"]),
        {"schema": "raw"},
    )

    repo_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    sha: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    previous_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    additions: Mapped[int | None] = mapped_column(Integer)
    deletions: Mapped[int | None] = mapped_column(Integer)
    patch: Mapped[str | None] = mapped_column(Text)
    patch_truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class FileSnapshot(Base):
    """File content at a sha, inline or as a path to stored content."""

    __tablename__ = "file_snapshots"
    __table_args__ = {"schema": "raw"}

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("raw.repos.id"), primary_key=True, autoincrement=False
    )
    sha: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str | None] = mapped_column(Text)
    storage_path: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(TZ)


class Log(Base):
    """Where a job's sanitised log is stored."""

    __tablename__ = "logs"
    __table_args__ = {"schema": "raw"}

    job_id: Mapped[int] = mapped_column(
        ForeignKey("raw.jobs.job_id"), primary_key=True, autoincrement=False
    )
    storage_path: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    redactions: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)  # ok | expired | missing
    fetched_at: Mapped[datetime | None] = mapped_column(TZ)


# ------------------------------------------------------------ platform schema
# DEFAULT: platform tables are keyed by the contract's own UUID id and keep the whole contract
# message in `payload` (jsonb) beside the indexed columns listed in DATA.md 4.2.


class CommitContextRow(Base):
    """A stored CommitContext."""

    __tablename__ = "commit_contexts"
    __table_args__ = (
        Index("ix_commit_contexts_repo_sha", "repo", "head_sha"),
        {"schema": "platform"},
    )

    context_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    repo: Mapped[str] = mapped_column(Text)  # "owner/name"
    head_sha: Mapped[str] = mapped_column(Text, index=True)
    event: Mapped[str] = mapped_column(Text, index=True)
    history_cutoff: Mapped[datetime] = mapped_column(TZ, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class PredictionRow(Base):
    """A stored RiskOutput."""

    __tablename__ = "predictions"
    __table_args__ = (Index("ix_predictions_context", "context_id"), {"schema": "platform"})

    prediction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    context_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    expert: Mapped[str] = mapped_column(Text, index=True)
    probability: Mapped[float | None] = mapped_column(Float, index=True)
    alert: Mapped[bool] = mapped_column(Boolean, index=True)
    is_replay: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class RoutedFailureRow(Base):
    """A stored RoutedFailure."""

    __tablename__ = "routed_failures"
    __table_args__ = (
        Index("ix_routed_failures_job", "run_id", "attempt", "job_id"),
        {"schema": "platform"},
    )

    failure_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger)
    attempt: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[int] = mapped_column(BigInteger, index=True)
    replay: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class DiagnosisRow(Base):
    """A stored Diagnosis."""

    __tablename__ = "diagnoses"
    __table_args__ = (Index("ix_diagnoses_failure", "failure_id"), {"schema": "platform"})

    diagnosis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    failure_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    expert: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(Text, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ReviewRow(Base):
    """A stored ReviewDecision."""

    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_diagnosis", "diagnosis_id"), {"schema": "platform"})

    decision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    diagnosis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    decision: Mapped[str] = mapped_column(Text, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class FailureLabelRow(Base):
    """A stored FailureLabel (several labels may exist per job; one is final)."""

    __tablename__ = "failure_labels"
    __table_args__ = (
        Index("ix_failure_labels_job", "run_id", "attempt", "job_id"),
        {"schema": "platform"},
    )

    # DEFAULT: the contract has no id of its own, so labels get a surrogate bigint key.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BigInteger)
    attempt: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[int] = mapped_column(BigInteger, index=True)
    category: Mapped[str] = mapped_column(Text, index=True)
    sub_label: Mapped[str | None] = mapped_column(Text, index=True)
    labelled_by: Mapped[str] = mapped_column(Text, index=True)
    is_final: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ApiKey(Base):
    """A hashed API key for the pre-push hook."""

    __tablename__ = "api_keys"
    __table_args__ = {"schema": "platform"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_hash: Mapped[str] = mapped_column(Text, index=True)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ, index=True)


class LlmCall(Base):
    """One LLM call, for cost and latency tracking."""

    __tablename__ = "llm_calls"
    __table_args__ = {"schema": "platform"}

    # DEFAULT: surrogate bigint key; llm_calls has no natural id
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    expert: Mapped[str] = mapped_column(Text, index=True)
    model: Mapped[str] = mapped_column(Text, index=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, index=True)
    output_tokens: Mapped[int] = mapped_column(Integer, index=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), index=True)
    cache_hit: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    latency_ms: Mapped[int] = mapped_column(Integer, index=True)
