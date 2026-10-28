"""SqlStore against a real Postgres (integration: auto-skipped when the DB is unreachable)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from api.auth import hash_key
from api.store import SqlStore
from api_db import engine  # noqa: F401
from api_helpers import USER_HASH, error_diagnosis, payload_json, risk_for, routed_failure
from features.pre_push import PrePushPayload, build_pre_push_context
from sqlalchemy import Engine, delete

from shared.contracts import Expert, ReviewDecision, RiskOutput
from shared.db.models import (
    ApiKey,
    CommitContextRow,
    DiagnosisRow,
    PredictionRow,
    ReviewRow,
    RoutedFailureRow,
)

pytestmark = pytest.mark.integration


def test_sql_store_round_trip(engine: Engine) -> None:
    store = SqlStore(engine)
    key_hash = hash_key("piq_fake-" + uuid4().hex)
    ctx = build_pre_push_context(
        PrePushPayload.model_validate(payload_json()), author_hash=USER_HASH
    )
    failure = routed_failure()
    diag = error_diagnosis(failure.failure_id)
    decision = ReviewDecision(
        diagnosis_id=diag.diagnosis_id,
        decision="accept",
        reviewer_hash=USER_HASH,
        cause_confirmed=True,
    )
    risk = RiskOutput.model_validate(risk_for(Expert.TEST, {"context_id": str(ctx.context_id)}))
    key = store.add_key(USER_HASH, key_hash)
    try:
        found = store.find_active_key(key_hash)
        assert found is not None and found.id == key.id
        store.save_context(ctx)
        store.save_predictions([risk])
        store.save_routed_failure(failure)
        store.save_diagnosis(diag)
        store.save_review(decision)

        risks = store.risks_for_commit(ctx.head_sha, repo=None, include_replay=False)
        assert risk.prediction_id in [r.prediction_id for r in risks]
        runs = store.list_runs(repo=ctx.repo.full_name, limit=500)
        assert any(i.context_id == ctx.context_id for i in runs)
        got = store.get_failure(failure.failure_id)
        assert got is not None and got[1][0].diagnosis_id == diag.diagnosis_id
        assert got[2][0].decision_id == decision.decision_id

        assert store.revoke_key(key.id) is True
        assert store.find_active_key(key_hash) is None
        assert store.revoke_key(uuid4()) is False
    finally:
        with engine.begin() as conn:
            conn.execute(delete(ReviewRow).where(ReviewRow.decision_id == decision.decision_id))
            conn.execute(delete(DiagnosisRow).where(DiagnosisRow.diagnosis_id == diag.diagnosis_id))
            conn.execute(
                delete(RoutedFailureRow).where(RoutedFailureRow.failure_id == failure.failure_id)
            )
            conn.execute(delete(PredictionRow).where(PredictionRow.context_id == ctx.context_id))
            conn.execute(
                delete(CommitContextRow).where(CommitContextRow.context_id == ctx.context_id)
            )
            conn.execute(delete(ApiKey).where(ApiKey.id == key.id))


def test_sql_raw_store_writes_app_rows(engine: Engine) -> None:
    """App-sourced rows land in raw with source="app"; an existing row keeps its source."""
    from api.raw_store import AppJob, AppRun, SqlRawStore
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from shared.db.models import Job, Step, WorkflowRun

    raw = SqlRawStore(engine)
    raw.upsert_repo(990001, "acme/app-test", "main")
    run = AppRun(
        run_id=990002, attempt=1, repo_id=990001, workflow_path="ci.yml", head_sha="c" * 40,
        head_branch="main", event="push", status="in_progress", conclusion=None,
        created_at=None, run_started_at=None, updated_at=None, actor_hash=None,
        pr_numbers=[3], raw={"id": 990002},
    )  # fmt: skip
    raw.upsert_run(run)
    raw.upsert_run(AppRun(**{**run.__dict__, "status": "completed", "conclusion": "failure"}))
    job = AppJob(
        job_id=990003, run_id=990002, attempt=1, name="test", status="completed",
        conclusion="failure", started_at=None, completed_at=None, runner_os="Linux",
        labels=["ubuntu-latest"], raw={"id": 990003},
        steps=[{"number": 1, "name": "npm test", "status": "completed", "conclusion": "failure",
                "started_at": None, "completed_at": None}],
    )  # fmt: skip
    raw.upsert_job(job)
    raw.upsert_job(job)  # idempotent
    with Session(engine) as s:
        row = s.execute(select(WorkflowRun).where(WorkflowRun.run_id == 990002)).scalar_one()
        assert (row.source, row.status, row.conclusion, row.pr_numbers) == (
            "app", "completed", "failure", [3]
        )  # fmt: skip
        assert s.execute(select(Job).where(Job.job_id == 990003)).scalar_one().runner_os == "Linux"
        assert len(s.execute(select(Step).where(Step.job_id == 990003)).all()) == 1
