"""Persistence for the `platform` schema, behind a small interface (SQL and in-memory)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from shared.contracts import CommitContext, Diagnosis, ReviewDecision, RiskOutput, RoutedFailure
from shared.db.models import (
    ApiKey,
    CommitContextRow,
    DiagnosisRow,
    PredictionRow,
    ReviewRow,
    RoutedFailureRow,
    WorkflowRun,
)
from shared.db.models import Repo as RepoRow

from .schemas import FailureItem, RunItem


def _json(model: Any) -> dict[str, Any]:
    """Contract to a JSON-safe dict (UUIDs and datetimes as strings)."""
    dumped: dict[str, Any] = model.model_dump(mode="json")
    return dumped


def _failure_item(
    failure: RoutedFailure, diagnosis_count: int, reviews: list[ReviewDecision]
) -> FailureItem:
    latest = max(reviews, key=lambda r: r.decided_at).decision if reviews else None
    return FailureItem(
        failure_id=failure.failure_id,
        repo=failure.repo.full_name,
        head_sha=failure.head_sha,
        run_id=failure.run_id,
        job_name=failure.job_name,
        expert=failure.sent_to[0],
        created_at=failure.routed_at,
        diagnosis_count=diagnosis_count,
        latest_review=latest,
    )


@dataclass(frozen=True)
class KeyRecord:
    """A stored API key (the hash only)."""

    id: uuid.UUID
    user_hash: str
    key_hash: str
    created_at: datetime
    revoked_at: datetime | None = None


class Store(Protocol):
    """Everything the API reads and writes."""

    def add_key(self, user_hash: str, key_hash: str) -> KeyRecord:
        """Save a new hashed key."""
        ...

    def find_active_key(self, key_hash: str) -> KeyRecord | None:
        """Return the key with this hash unless it is revoked."""
        ...

    def revoke_key(self, key_id: uuid.UUID) -> bool:
        """Revoke a key; False when it does not exist."""
        ...

    def list_keys(self, user_hash: str) -> list[KeyRecord]:
        """All keys (revoked too) of one user, newest first."""
        ...

    def list_failures(self, *, repo: str | None, limit: int) -> list[FailureItem]:
        """Routed failures (not replay), newest first, with diagnosis count and latest review."""
        ...

    def save_context(self, context: CommitContext) -> None:
        """Save a CommitContext."""
        ...

    def save_predictions(self, risks: list[RiskOutput], *, is_replay: bool = False) -> None:
        """Save RiskOutputs."""
        ...

    def save_diagnosis(self, diagnosis: Diagnosis) -> None:
        """Save a Diagnosis."""
        ...

    def save_routed_failure(self, failure: RoutedFailure) -> None:
        """Save a RoutedFailure (written by the router in P2.7)."""
        ...

    def save_review(self, decision: ReviewDecision) -> None:
        """Save a ReviewDecision."""
        ...

    def get_diagnosis(self, diagnosis_id: uuid.UUID) -> Diagnosis | None:
        """Return one diagnosis."""
        ...

    def risks_for_commit(
        self, head_sha: str, *, repo: str | None, include_replay: bool
    ) -> list[RiskOutput]:
        """Predictions for contexts of this commit, newest first."""
        ...

    def list_runs(self, *, repo: str | None, limit: int) -> list[RunItem]:
        """Recent contexts with predictions and the matching run outcome."""
        ...

    def get_failure(
        self, failure_id: uuid.UUID
    ) -> tuple[RoutedFailure, list[Diagnosis], list[ReviewDecision]] | None:
        """A failure with its diagnoses and their reviews."""
        ...


class SqlStore:
    """`platform` tables in Postgres (contracts kept whole in `payload`)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def add_key(self, user_hash: str, key_hash: str) -> KeyRecord:
        """Save a new hashed key."""
        key_id = uuid.uuid4()
        created = datetime.now(UTC)
        with Session(self._engine) as s, s.begin():
            s.add(ApiKey(id=key_id, user_hash=user_hash, key_hash=key_hash, created_at=created))
        return KeyRecord(key_id, user_hash, key_hash, created)

    def find_active_key(self, key_hash: str) -> KeyRecord | None:
        """Return the key with this hash unless it is revoked."""
        with Session(self._engine) as s:
            row = s.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
            ).scalar_one_or_none()
            if row is None:
                return None
            return KeyRecord(row.id, row.user_hash, row.key_hash, row.created_at, row.revoked_at)

    def revoke_key(self, key_id: uuid.UUID) -> bool:
        """Revoke a key; False when it does not exist."""
        with Session(self._engine) as s, s.begin():
            row = s.get(ApiKey, key_id)
            if row is None:
                return False
            if row.revoked_at is None:
                s.execute(
                    update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=datetime.now(UTC))
                )
            return True

    def list_keys(self, user_hash: str) -> list[KeyRecord]:
        """All keys (revoked too) of one user, newest first."""
        with Session(self._engine) as s:
            rows = s.execute(
                select(ApiKey)
                .where(ApiKey.user_hash == user_hash)
                .order_by(ApiKey.created_at.desc())
            ).scalars()
            return [
                KeyRecord(r.id, r.user_hash, r.key_hash, r.created_at, r.revoked_at) for r in rows
            ]

    def list_failures(self, *, repo: str | None, limit: int) -> list[FailureItem]:
        """Routed failures (not replay), newest first, with diagnosis count and latest review."""
        stmt = (
            select(RoutedFailureRow.payload)
            .where(RoutedFailureRow.replay.is_(False))
            .order_by(RoutedFailureRow.payload["routed_at"].astext.desc())
            .limit(limit)
        )
        if repo:
            owner, _, name = repo.partition("/")
            stmt = stmt.where(
                RoutedFailureRow.payload["repo"]["owner"].astext == owner,
                RoutedFailureRow.payload["repo"]["name"].astext == name,
            )
        items: list[FailureItem] = []
        with Session(self._engine) as s:
            failures = [RoutedFailure.model_validate(p) for p in s.execute(stmt).scalars()]
            for f in failures:
                dids = (
                    s.execute(
                        select(DiagnosisRow.diagnosis_id).where(
                            DiagnosisRow.failure_id == f.failure_id
                        )
                    )
                    .scalars()
                    .all()
                )
                reviews = (
                    [
                        ReviewDecision.model_validate(p)
                        for p in s.execute(
                            select(ReviewRow.payload).where(ReviewRow.diagnosis_id.in_(dids))
                        ).scalars()
                    ]
                    if dids
                    else []
                )
                items.append(_failure_item(f, len(dids), reviews))
        return items

    def save_context(self, context: CommitContext) -> None:
        """Save a CommitContext."""
        with Session(self._engine) as s, s.begin():
            s.merge(
                CommitContextRow(
                    context_id=context.context_id,
                    repo=context.repo.full_name,
                    head_sha=context.head_sha,
                    event=context.event,
                    history_cutoff=context.history_cutoff,
                    payload=_json(context),
                )
            )

    def save_predictions(self, risks: list[RiskOutput], *, is_replay: bool = False) -> None:
        """Save RiskOutputs."""
        with Session(self._engine) as s, s.begin():
            for r in risks:
                s.merge(
                    PredictionRow(
                        prediction_id=r.prediction_id,
                        context_id=r.context_id,
                        expert=r.expert.value,
                        probability=r.probability,
                        alert=r.alert,
                        is_replay=is_replay,
                        payload=_json(r),
                    )
                )

    def save_diagnosis(self, diagnosis: Diagnosis) -> None:
        """Save a Diagnosis."""
        with Session(self._engine) as s, s.begin():
            s.merge(
                DiagnosisRow(
                    diagnosis_id=diagnosis.diagnosis_id,
                    failure_id=diagnosis.failure_id,
                    expert=diagnosis.expert.value,
                    status=diagnosis.status,
                    payload=_json(diagnosis),
                )
            )

    def save_routed_failure(self, failure: RoutedFailure) -> None:
        """Save a RoutedFailure."""
        with Session(self._engine) as s, s.begin():
            s.merge(
                RoutedFailureRow(
                    failure_id=failure.failure_id,
                    run_id=failure.run_id,
                    attempt=failure.run_attempt,
                    job_id=failure.job_id,
                    replay=failure.replay,
                    payload=_json(failure),
                )
            )

    def save_review(self, decision: ReviewDecision) -> None:
        """Save a ReviewDecision."""
        with Session(self._engine) as s, s.begin():
            s.merge(
                ReviewRow(
                    decision_id=decision.decision_id,
                    diagnosis_id=decision.diagnosis_id,
                    decision=decision.decision,
                    payload=_json(decision),
                )
            )

    def get_diagnosis(self, diagnosis_id: uuid.UUID) -> Diagnosis | None:
        """Return one diagnosis."""
        with Session(self._engine) as s:
            row = s.get(DiagnosisRow, diagnosis_id)
            return Diagnosis.model_validate(row.payload) if row else None

    def risks_for_commit(
        self, head_sha: str, *, repo: str | None, include_replay: bool
    ) -> list[RiskOutput]:
        """Predictions for contexts of this commit, newest first."""
        stmt = (
            select(PredictionRow.payload)
            .join(CommitContextRow, CommitContextRow.context_id == PredictionRow.context_id)
            .where(CommitContextRow.head_sha == head_sha)
        )
        if repo:
            stmt = stmt.where(CommitContextRow.repo == repo)
        if not include_replay:
            stmt = stmt.where(PredictionRow.is_replay.is_(False))
        with Session(self._engine) as s:
            payloads = s.execute(stmt).scalars().all()
        risks = [RiskOutput.model_validate(p) for p in payloads]
        return sorted(risks, key=lambda r: r.created_at, reverse=True)

    def list_runs(self, *, repo: str | None, limit: int) -> list[RunItem]:
        """Recent contexts with predictions and the matching run outcome."""
        stmt = (
            select(CommitContextRow)
            .where(CommitContextRow.event != "replay")
            .order_by(CommitContextRow.history_cutoff.desc())
            .limit(limit)
        )
        if repo:
            stmt = stmt.where(CommitContextRow.repo == repo)
        items: list[RunItem] = []
        with Session(self._engine) as s:
            for ctx in s.execute(stmt).scalars():
                preds = (
                    s.execute(
                        select(PredictionRow.payload).where(
                            PredictionRow.context_id == ctx.context_id,
                            PredictionRow.is_replay.is_(False),
                        )
                    )
                    .scalars()
                    .all()
                )
                # Latest attempt of the run for this commit, if the collector or App has seen it.
                run = s.execute(
                    select(WorkflowRun)
                    .join(RepoRow, RepoRow.id == WorkflowRun.repo_id)
                    .where(RepoRow.full_name == ctx.repo, WorkflowRun.head_sha == ctx.head_sha)
                    .order_by(WorkflowRun.created_at.desc(), WorkflowRun.attempt.desc())
                    .limit(1)
                ).scalar_one_or_none()
                items.append(
                    RunItem(
                        context_id=ctx.context_id,
                        repo=ctx.repo,
                        head_sha=ctx.head_sha,
                        event=ctx.event,
                        history_cutoff=ctx.history_cutoff,
                        predictions=[RiskOutput.model_validate(p) for p in preds],
                        run_id=run.run_id if run else None,
                        run_status=run.status if run else None,
                        run_conclusion=run.conclusion if run else None,
                    )
                )
        return items

    def get_failure(
        self, failure_id: uuid.UUID
    ) -> tuple[RoutedFailure, list[Diagnosis], list[ReviewDecision]] | None:
        """A failure with its diagnoses and their reviews."""
        with Session(self._engine) as s:
            frow = s.get(RoutedFailureRow, failure_id)
            if frow is None:
                return None
            drows = (
                s.execute(select(DiagnosisRow.payload).where(DiagnosisRow.failure_id == failure_id))
                .scalars()
                .all()
            )
            diagnoses = [Diagnosis.model_validate(p) for p in drows]
            ids = [d.diagnosis_id for d in diagnoses]
            rrows = (
                s.execute(select(ReviewRow.payload).where(ReviewRow.diagnosis_id.in_(ids)))
                .scalars()
                .all()
                if ids
                else []
            )
            return (
                RoutedFailure.model_validate(frow.payload),
                diagnoses,
                [ReviewDecision.model_validate(p) for p in rrows],
            )


class InMemoryStore:
    """Dict-backed store for unit tests."""

    def __init__(self) -> None:
        self.keys: dict[uuid.UUID, KeyRecord] = {}
        self.contexts: dict[uuid.UUID, CommitContext] = {}
        self.predictions: dict[uuid.UUID, tuple[RiskOutput, bool]] = {}
        self.diagnoses: dict[uuid.UUID, Diagnosis] = {}
        self.failures: dict[uuid.UUID, RoutedFailure] = {}
        self.reviews: dict[uuid.UUID, ReviewDecision] = {}
        # (repo, head_sha) -> (run_id, status, conclusion); tests fill it to fake raw.workflow_runs
        self.run_outcomes: dict[tuple[str, str], tuple[int, str | None, str | None]] = {}

    def add_key(self, user_hash: str, key_hash: str) -> KeyRecord:
        """Save a new hashed key."""
        rec = KeyRecord(uuid.uuid4(), user_hash, key_hash, datetime.now(UTC))
        self.keys[rec.id] = rec
        return rec

    def find_active_key(self, key_hash: str) -> KeyRecord | None:
        """Return the key with this hash unless it is revoked."""
        for rec in self.keys.values():
            if rec.key_hash == key_hash and rec.revoked_at is None:
                return rec
        return None

    def revoke_key(self, key_id: uuid.UUID) -> bool:
        """Revoke a key; False when it does not exist."""
        rec = self.keys.get(key_id)
        if rec is None:
            return False
        if rec.revoked_at is None:
            self.keys[key_id] = KeyRecord(
                rec.id, rec.user_hash, rec.key_hash, rec.created_at, datetime.now(UTC)
            )
        return True

    def list_keys(self, user_hash: str) -> list[KeyRecord]:
        """All keys (revoked too) of one user, newest first."""
        recs = [k for k in self.keys.values() if k.user_hash == user_hash]
        return sorted(recs, key=lambda k: k.created_at, reverse=True)

    def list_failures(self, *, repo: str | None, limit: int) -> list[FailureItem]:
        """Routed failures (not replay), newest first, with diagnosis count and latest review."""
        found = sorted(
            (
                f
                for f in self.failures.values()
                if not f.replay and (repo is None or f.repo.full_name == repo)
            ),
            key=lambda f: f.routed_at,
            reverse=True,
        )[:limit]
        items: list[FailureItem] = []
        for f in found:
            dids = {d.diagnosis_id for d in self.diagnoses.values() if d.failure_id == f.failure_id}
            reviews = [r for r in self.reviews.values() if r.diagnosis_id in dids]
            items.append(_failure_item(f, len(dids), reviews))
        return items

    def save_context(self, context: CommitContext) -> None:
        """Save a CommitContext."""
        self.contexts[context.context_id] = context

    def save_predictions(self, risks: list[RiskOutput], *, is_replay: bool = False) -> None:
        """Save RiskOutputs."""
        for r in risks:
            self.predictions[r.prediction_id] = (r, is_replay)

    def save_diagnosis(self, diagnosis: Diagnosis) -> None:
        """Save a Diagnosis."""
        self.diagnoses[diagnosis.diagnosis_id] = diagnosis

    def save_routed_failure(self, failure: RoutedFailure) -> None:
        """Save a RoutedFailure."""
        self.failures[failure.failure_id] = failure

    def save_review(self, decision: ReviewDecision) -> None:
        """Save a ReviewDecision."""
        self.reviews[decision.decision_id] = decision

    def get_diagnosis(self, diagnosis_id: uuid.UUID) -> Diagnosis | None:
        """Return one diagnosis."""
        return self.diagnoses.get(diagnosis_id)

    def risks_for_commit(
        self, head_sha: str, *, repo: str | None, include_replay: bool
    ) -> list[RiskOutput]:
        """Predictions for contexts of this commit, newest first."""
        ids = {
            c.context_id
            for c in self.contexts.values()
            if c.head_sha == head_sha and (repo is None or c.repo.full_name == repo)
        }
        risks = [
            r
            for r, replay in self.predictions.values()
            if r.context_id in ids and (include_replay or not replay)
        ]
        return sorted(risks, key=lambda r: r.created_at, reverse=True)

    def list_runs(self, *, repo: str | None, limit: int) -> list[RunItem]:
        """Recent contexts with predictions and the matching run outcome."""
        ctxs = sorted(
            (
                c
                for c in self.contexts.values()
                if c.event != "replay" and (repo is None or c.repo.full_name == repo)
            ),
            key=lambda c: c.history_cutoff,
            reverse=True,
        )[:limit]
        items: list[RunItem] = []
        for c in ctxs:
            outcome = self.run_outcomes.get((c.repo.full_name, c.head_sha))
            items.append(
                RunItem(
                    context_id=c.context_id,
                    repo=c.repo.full_name,
                    head_sha=c.head_sha,
                    event=c.event,
                    history_cutoff=c.history_cutoff,
                    predictions=[
                        r
                        for r, replay in self.predictions.values()
                        if r.context_id == c.context_id and not replay
                    ],
                    run_id=outcome[0] if outcome else None,
                    run_status=outcome[1] if outcome else None,
                    run_conclusion=outcome[2] if outcome else None,
                )
            )
        return items

    def get_failure(
        self, failure_id: uuid.UUID
    ) -> tuple[RoutedFailure, list[Diagnosis], list[ReviewDecision]] | None:
        """A failure with its diagnoses and their reviews."""
        failure = self.failures.get(failure_id)
        if failure is None:
            return None
        diagnoses = [d for d in self.diagnoses.values() if d.failure_id == failure_id]
        ids = {d.diagnosis_id for d in diagnoses}
        return failure, diagnoses, [r for r in self.reviews.values() if r.diagnosis_id in ids]
