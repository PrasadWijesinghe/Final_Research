"""Writes GitHub App data into the `raw` schema (`source="app"`), SQL and in-memory versions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import Engine, Table, null
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session

from shared.db import models as m


@dataclass(frozen=True)
class AppRun:
    """A row of raw.workflow_runs written from a webhook."""

    run_id: int
    attempt: int
    repo_id: int
    workflow_path: str | None
    head_sha: str
    head_branch: str | None
    event: str | None
    status: str | None
    conclusion: str | None
    created_at: datetime | None
    run_started_at: datetime | None
    updated_at: datetime | None
    actor_hash: str | None
    pr_numbers: list[int] = field(default_factory=list)
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class AppJob:
    """A row of raw.jobs plus its steps (number, name, status, conclusion, started, completed)."""

    job_id: int
    run_id: int
    attempt: int
    name: str | None
    status: str | None
    conclusion: str | None
    started_at: datetime | None
    completed_at: datetime | None
    runner_os: str | None
    labels: list[str]
    raw: dict[str, Any] | None
    steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AppLog:
    """A row of raw.logs for a log the webhook handler fetched."""

    job_id: int
    storage_path: str
    bytes: int
    sha256: str
    redactions: int
    fetched_at: datetime


class RawStore(Protocol):
    """What the webhook handler writes to `raw`."""

    def upsert_repo(self, repo_id: int, full_name: str, default_branch: str | None) -> None:
        """Create the repo row if missing."""
        ...

    def upsert_run(self, run: AppRun) -> None:
        """Insert a run attempt or update it (the `source` of an existing row is kept)."""
        ...

    def upsert_job(self, job: AppJob) -> None:
        """Insert or update a job and replace its steps."""
        ...

    def upsert_log(self, log: AppLog) -> None:
        """Insert or update the log row of a job."""
        ...


class SqlRawStore:
    """Postgres implementation."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _upsert(
        s: Session,
        model: type[m.Base],
        values: dict[str, Any],
        keys: list[str],
        keep: tuple[str, ...] = (),
    ) -> None:
        table = cast(Table, model.__table__)
        values = {
            k: null() if v is None and isinstance(table.c[k].type, JSONB) else v
            for k, v in values.items()
        }
        stmt = insert(table).values(**values)
        changed = {k: stmt.excluded[k] for k in values if k not in keys and k not in keep}
        if changed:
            stmt = stmt.on_conflict_do_update(index_elements=keys, set_=changed)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=keys)
        s.execute(stmt)

    def upsert_repo(self, repo_id: int, full_name: str, default_branch: str | None) -> None:
        """Create the repo row if missing (an existing row, e.g. the collector's, is untouched)."""
        with Session(self._engine) as s, s.begin():
            stmt = insert(cast(Table, m.Repo.__table__)).values(
                id=repo_id, full_name=full_name, default_branch=default_branch, enabled=True
            )
            s.execute(stmt.on_conflict_do_nothing())

    def upsert_run(self, run: AppRun) -> None:
        """Insert a run attempt or update it."""
        values = {**run.__dict__, "source": "app"}
        with Session(self._engine) as s, s.begin():
            self._upsert(s, m.WorkflowRun, values, ["run_id", "attempt"], keep=("source",))

    def upsert_job(self, job: AppJob) -> None:
        """Insert or update a job and its steps."""
        values = {k: v for k, v in job.__dict__.items() if k != "steps"}
        with Session(self._engine) as s, s.begin():
            self._upsert(s, m.Job, values, ["job_id"])
            for step in job.steps:
                self._upsert(s, m.Step, {"job_id": job.job_id, **step}, ["job_id", "number"])

    def upsert_log(self, log: AppLog) -> None:
        """Insert or update a raw.logs row."""
        with Session(self._engine) as s, s.begin():
            self._upsert(s, m.Log, {**log.__dict__, "status": "ok"}, ["job_id"])


class InMemoryRawStore:
    """Dict-backed store for unit tests."""

    def __init__(self) -> None:
        self.repos: dict[int, str] = {}
        self.runs: dict[tuple[int, int], AppRun] = {}
        self.jobs: dict[int, AppJob] = {}
        self.logs: dict[int, AppLog] = {}

    def upsert_repo(self, repo_id: int, full_name: str, default_branch: str | None) -> None:
        """Create the repo row if missing."""
        self.repos.setdefault(repo_id, full_name)

    def upsert_run(self, run: AppRun) -> None:
        """Insert or replace a run attempt."""
        self.runs[(run.run_id, run.attempt)] = run

    def upsert_job(self, job: AppJob) -> None:
        """Insert or replace a job."""
        self.jobs[job.job_id] = job

    def upsert_log(self, log: AppLog) -> None:
        """Insert or replace a log row."""
        self.logs[log.job_id] = log
