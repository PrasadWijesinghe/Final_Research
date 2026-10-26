"""Storage for the collector: plain records, a small `Store` interface and the Postgres version.

The backfill logic only talks to `Store`, so it can be tested with an in-memory fake. `SqlStore`
writes the `raw` tables from `shared.db.models` with `INSERT ... ON CONFLICT DO UPDATE`, keyed by
GitHub's own ids, so every write is an upsert.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, cast

from sqlalchemy import (
    Connection,
    Date,
    Engine,
    Table,
    and_,
    exists,
    func,
    literal_column,
    null,
    or_,
    select,
)
from sqlalchemy import (
    cast as sa_cast,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from collector.policy import FAILED_CONCLUSIONS, LOG_CONCLUSIONS, PASSED_CONCLUSIONS
from shared.db import models as m


@dataclass(frozen=True)
class RepoRecord:
    """A row of raw.repos."""

    id: int
    full_name: str
    member: str | None
    licence: str | None
    default_branch: str | None
    enabled: bool = True


@dataclass(frozen=True)
class SyncStateRecord:
    """A row of raw.sync_state."""

    repo_id: int
    backfill_done_at: datetime | None = None
    backfill_cursor: str | None = None
    last_daily_sync_at: datetime | None = None


@dataclass(frozen=True)
class SkippedRunRecord:
    """A row of raw.skipped_runs: a run given up on after persistent GitHub 5xx errors."""

    repo_id: int
    run_id: int
    attempt: int
    reason: str
    skipped_at: datetime


@dataclass(frozen=True)
class WorkflowRecord:
    """A row of raw.workflows."""

    repo_id: int
    path: str
    name: str | None
    relevant: bool
    detected_at_sha: str | None


@dataclass(frozen=True)
class RunRecord:
    """A row of raw.workflow_runs (one attempt)."""

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
    source: str = "collector"
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class JobRecord:
    """A row of raw.jobs."""

    job_id: int
    run_id: int
    attempt: int
    name: str | None
    job_key: str | None
    status: str | None
    conclusion: str | None
    started_at: datetime | None
    completed_at: datetime | None
    runner_os: str | None
    labels: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class StepRecord:
    """A row of raw.steps."""

    job_id: int
    number: int
    name: str | None
    status: str | None
    conclusion: str | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class CommitRecord:
    """A row of raw.commits."""

    repo_id: int
    sha: str
    parent_shas: list[str]
    author_hash: str | None
    committed_at: datetime | None
    message: str | None


@dataclass(frozen=True)
class CommitFileRecord:
    """A row of raw.commit_files."""

    repo_id: int
    sha: str
    path: str
    previous_path: str | None
    status: str | None
    additions: int | None
    deletions: int | None
    patch: str | None
    patch_truncated: bool = False


@dataclass(frozen=True)
class SnapshotRecord:
    """A row of raw.file_snapshots (inline `content`, or `storage_path` for lockfiles)."""

    repo_id: int
    sha: str
    path: str
    content: str | None
    storage_path: str | None
    fetched_at: datetime | None


@dataclass(frozen=True)
class LogRecord:
    """A row of raw.logs."""

    job_id: int
    storage_path: str | None
    bytes: int | None
    sha256: str | None
    redactions: int | None
    status: str  # ok | expired | missing
    fetched_at: datetime | None


@dataclass(frozen=True)
class RepoCounts:
    """What is stored for one repo (used by `collector status`)."""

    runs: int = 0  # distinct workflow runs
    attempts: int = 0  # rows of raw.workflow_runs
    jobs: int = 0
    logs: dict[str, int] = field(default_factory=dict)  # status -> number of logs
    commits: int = 0
    logs_pending: int = 0  # jobs the log policy wants a log for, with no raw.logs row yet
    runs_skipped: int = 0  # runs given up on after GitHub 5xx errors, retried on later runs


@dataclass(frozen=True)
class PendingLog:
    """A job that needs a log fetched (see `Store.pending_logs`)."""

    job_id: int
    run_id: int
    attempt: int


class Store(Protocol):
    """What the backfill needs from a database. All calls must run inside `transaction()`."""

    def transaction(self) -> AbstractContextManager[None]:
        """Group writes: commit on success, roll back if the block raises."""
        ...

    def upsert_repo(self, repo: RepoRecord) -> None: ...
    def ensure_sync_state(self, repo_id: int) -> None: ...
    def get_sync_state(self, repo_id: int) -> SyncStateRecord | None: ...
    def set_cursor(self, repo_id: int, cursor: str | None) -> None: ...
    def finish_backfill(self, repo_id: int, done_at: datetime, daily_at: datetime) -> None: ...
    def get_workflows(self, repo_id: int) -> dict[str, WorkflowRecord]: ...
    def upsert_workflow(self, workflow: WorkflowRecord) -> None: ...
    def attempt_done(self, repo_id: int, run_id: int, attempt: int) -> bool: ...
    def upsert_run(self, run: RunRecord) -> None: ...
    def upsert_job(self, job: JobRecord) -> None: ...
    def upsert_steps(self, steps: Sequence[StepRecord]) -> None: ...
    def has_commit(self, repo_id: int, sha: str) -> bool: ...
    def upsert_commit(self, commit: CommitRecord, files: Sequence[CommitFileRecord]) -> None: ...
    def commit_files(self, repo_id: int, sha: str) -> list[tuple[str, str | None]]: ...
    def get_snapshot(self, repo_id: int, sha: str, path: str) -> SnapshotRecord | None: ...
    def upsert_snapshot(self, snapshot: SnapshotRecord) -> None: ...
    def has_log(self, job_id: int) -> bool: ...
    def upsert_log(self, log: LogRecord) -> None: ...
    def pending_logs(
        self, repo_id: int, limit: int | None, *, failed_runs_only: bool = False
    ) -> list[PendingLog]:
        """Jobs needing a log, newest run first.

        `failed_runs_only`: a passing job counts only if some job of the same run (any attempt)
        failed. It narrows the list before `limit` applies.
        """
        ...

    def job_names(self, run_id: int, attempt: int) -> list[tuple[int, str | None]]:
        """(job id, name) of every job stored for one run attempt."""
        ...

    def add_skipped_run(self, skipped: SkippedRunRecord) -> None: ...
    def clear_skipped_run(self, repo_id: int, run_id: int) -> None: ...
    def skipped_runs(self, repo_id: int) -> list[SkippedRunRecord]: ...
    def set_last_daily_sync(self, repo_id: int, at: datetime) -> None: ...
    def list_repos(self, member: str) -> list[RepoRecord]: ...
    def repo_counts(self, repo_id: int) -> RepoCounts: ...
    def run_days(self, repo_id: int) -> list[date]: ...


def _table(model: type[m.Base]) -> Table:
    return cast(Table, model.__table__)


class SqlStore:
    """`Store` on Postgres through SQLAlchemy Core (one connection per transaction)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._conn: Connection | None = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Open a transaction; a nested call joins the outer one."""
        if self._conn is not None:
            yield
            return
        with self._engine.begin() as conn:
            self._conn = conn
            try:
                yield
            finally:
                self._conn = None

    @property
    def _c(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("SqlStore used outside transaction()")
        return self._conn

    def _upsert(
        self,
        model: type[m.Base],
        values: dict[str, Any],
        keys: Sequence[str],
        *,
        update: bool,
        keep: Sequence[str] = (),
    ) -> None:
        table = _table(model)
        # Python None in a JSONB column would be stored as JSON null; we want SQL NULL.
        values = {
            k: null() if v is None and isinstance(table.c[k].type, JSONB) else v
            for k, v in values.items()
        }
        stmt = insert(table).values(**values)
        changed = {k: stmt.excluded[k] for k in values if k not in keys and k not in keep}
        if update and changed:
            stmt = stmt.on_conflict_do_update(index_elements=list(keys), set_=changed)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(keys))
        self._c.execute(stmt)

    def upsert_repo(self, repo: RepoRecord) -> None:
        """Insert or update a raw.repos row."""
        self._upsert(m.Repo, asdict(repo), ["id"], update=True)

    def ensure_sync_state(self, repo_id: int) -> None:
        """Create the sync_state row if it does not exist yet."""
        self._upsert(m.SyncState, {"repo_id": repo_id}, ["repo_id"], update=False)

    def get_sync_state(self, repo_id: int) -> SyncStateRecord | None:
        """Read the sync_state row."""
        t = _table(m.SyncState)
        row = self._c.execute(select(t).where(t.c.repo_id == repo_id)).mappings().first()
        return SyncStateRecord(**dict(row)) if row else None

    def set_cursor(self, repo_id: int, cursor: str | None) -> None:
        """Store the backfill cursor."""
        t = _table(m.SyncState)
        self._c.execute(t.update().where(t.c.repo_id == repo_id).values(backfill_cursor=cursor))

    def finish_backfill(self, repo_id: int, done_at: datetime, daily_at: datetime) -> None:
        """Clear the cursor; set backfill_done_at and last_daily_sync_at if they are still empty."""
        state = self.get_sync_state(repo_id)
        values: dict[str, Any] = {"backfill_cursor": None}
        if state is None or state.backfill_done_at is None:
            values["backfill_done_at"] = done_at
        if state is None or state.last_daily_sync_at is None:
            values["last_daily_sync_at"] = daily_at
        t = _table(m.SyncState)
        self._c.execute(t.update().where(t.c.repo_id == repo_id).values(**values))

    def get_workflows(self, repo_id: int) -> dict[str, WorkflowRecord]:
        """Read the stored workflows of a repo, keyed by path."""
        t = _table(m.Workflow)
        rows = self._c.execute(select(t).where(t.c.repo_id == repo_id)).mappings().all()
        return {r["path"]: WorkflowRecord(**dict(r)) for r in rows}

    def upsert_workflow(self, workflow: WorkflowRecord) -> None:
        """Insert or update a raw.workflows row."""
        self._upsert(m.Workflow, asdict(workflow), ["repo_id", "path"], update=True)

    def attempt_done(self, repo_id: int, run_id: int, attempt: int) -> bool:
        """True when this attempt is stored and its status is `completed`."""
        t = _table(m.WorkflowRun)
        found = self._c.execute(
            select(t.c.run_id).where(
                t.c.repo_id == repo_id,
                t.c.run_id == run_id,
                t.c.attempt == attempt,
                t.c.status == "completed",
            )
        ).first()
        return found is not None

    def upsert_run(self, run: RunRecord) -> None:
        """Insert or update a raw.workflow_runs row."""
        # `source` is written on first insert only, so a row owned by the GitHub App stays so.
        self._upsert(
            m.WorkflowRun, asdict(run), ["run_id", "attempt"], update=True, keep=("source",)
        )

    def upsert_job(self, job: JobRecord) -> None:
        """Insert or update a raw.jobs row."""
        self._upsert(m.Job, asdict(job), ["job_id"], update=True)

    def upsert_steps(self, steps: Sequence[StepRecord]) -> None:
        """Insert or update raw.steps rows."""
        for step in steps:
            self._upsert(m.Step, asdict(step), ["job_id", "number"], update=True)

    def has_commit(self, repo_id: int, sha: str) -> bool:
        """True when the commit row exists."""
        t = _table(m.Commit)
        found = self._c.execute(
            select(t.c.sha).where(t.c.repo_id == repo_id, t.c.sha == sha)
        ).first()
        return found is not None

    def upsert_commit(self, commit: CommitRecord, files: Sequence[CommitFileRecord]) -> None:
        """Insert or update a commit and its changed files."""
        self._upsert(m.Commit, asdict(commit), ["repo_id", "sha"], update=True)
        for f in files:
            self._upsert(m.CommitFile, asdict(f), ["repo_id", "sha", "path"], update=True)

    def commit_files(self, repo_id: int, sha: str) -> list[tuple[str, str | None]]:
        """Return (path, status) of the commit's files, sorted by path."""
        t = _table(m.CommitFile)
        rows = self._c.execute(
            select(t.c.path, t.c.status)
            .where(t.c.repo_id == repo_id, t.c.sha == sha)
            .order_by(t.c.path)
        ).all()
        return [(r[0], r[1]) for r in rows]

    def get_snapshot(self, repo_id: int, sha: str, path: str) -> SnapshotRecord | None:
        """Read one file snapshot."""
        t = _table(m.FileSnapshot)
        row = (
            self._c.execute(
                select(t).where(t.c.repo_id == repo_id, t.c.sha == sha, t.c.path == path)
            )
            .mappings()
            .first()
        )
        return SnapshotRecord(**dict(row)) if row else None

    def upsert_snapshot(self, snapshot: SnapshotRecord) -> None:
        """Insert or update a raw.file_snapshots row."""
        self._upsert(m.FileSnapshot, asdict(snapshot), ["repo_id", "sha", "path"], update=True)

    def has_log(self, job_id: int) -> bool:
        """True when a raw.logs row exists for the job (any status)."""
        t = _table(m.Log)
        return self._c.execute(select(t.c.job_id).where(t.c.job_id == job_id)).first() is not None

    def upsert_log(self, log: LogRecord) -> None:
        """Insert or update a raw.logs row."""
        self._upsert(m.Log, asdict(log), ["job_id"], update=True)

    def _pending_query(self, repo_id: int, *, failed_runs_only: bool = False) -> Any:
        wr, jb, lg = _table(m.WorkflowRun), _table(m.Job), _table(m.Log)
        on_run = and_(jb.c.run_id == wr.c.run_id, jb.c.attempt == wr.c.attempt)
        stmt = (
            select(jb.c.job_id, jb.c.run_id, jb.c.attempt)
            .select_from(jb.join(wr, on_run).outerjoin(lg, lg.c.job_id == jb.c.job_id))
            .where(
                wr.c.repo_id == repo_id,
                lg.c.job_id.is_(None),
                jb.c.status == "completed",
                jb.c.conclusion.in_(sorted(LOG_CONCLUSIONS)),
            )
        )
        if failed_runs_only:
            # Failed jobs always keep their log; a passing job's log only when its run (any
            # attempt) has a failed job, so a re-run that passed keeps its flip evidence (D40).
            sibling = _table(m.Job).alias("sibling")
            run_failed = exists().where(
                sibling.c.run_id == jb.c.run_id,
                sibling.c.conclusion.in_(sorted(FAILED_CONCLUSIONS)),
            )
            stmt = stmt.where(or_(jb.c.conclusion.not_in(sorted(PASSED_CONCLUSIONS)), run_failed))
        return stmt

    def pending_logs(
        self, repo_id: int, limit: int | None, *, failed_runs_only: bool = False
    ) -> list[PendingLog]:
        """Jobs needing a log with no raw.logs row, newest run first."""
        wr, jb = _table(m.WorkflowRun), _table(m.Job)
        query = self._pending_query(repo_id, failed_runs_only=failed_runs_only)
        stmt = query.order_by(
            wr.c.created_at.desc().nulls_last(),
            jb.c.run_id.desc(),
            jb.c.attempt.desc(),
            jb.c.job_id.desc(),
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [PendingLog(r[0], r[1], r[2]) for r in self._c.execute(stmt).all()]

    def job_names(self, run_id: int, attempt: int) -> list[tuple[int, str | None]]:
        """(job id, name) of every job stored for one run attempt, by job id."""
        jb = _table(m.Job)
        stmt = (
            select(jb.c.job_id, jb.c.name)
            .where(jb.c.run_id == run_id, jb.c.attempt == attempt)
            .order_by(jb.c.job_id)
        )
        return [(int(r[0]), r[1]) for r in self._c.execute(stmt).all()]

    def add_skipped_run(self, skipped: SkippedRunRecord) -> None:
        """Record (or refresh) a skipped run."""
        self._upsert(m.SkippedRun, asdict(skipped), ["repo_id", "run_id"], update=True)

    def clear_skipped_run(self, repo_id: int, run_id: int) -> None:
        """Forget a skipped run (no-op if it was not skipped)."""
        t = _table(m.SkippedRun)
        self._c.execute(t.delete().where(t.c.repo_id == repo_id, t.c.run_id == run_id))

    def skipped_runs(self, repo_id: int) -> list[SkippedRunRecord]:
        """The repo's skipped runs, newest run id first."""
        t = _table(m.SkippedRun)
        rows = (
            self._c.execute(select(t).where(t.c.repo_id == repo_id).order_by(t.c.run_id.desc()))
            .mappings()
            .all()
        )
        return [SkippedRunRecord(**dict(r)) for r in rows]

    def set_last_daily_sync(self, repo_id: int, at: datetime) -> None:
        """Record when the last successful daily sync of the repo started."""
        t = _table(m.SyncState)
        self._c.execute(t.update().where(t.c.repo_id == repo_id).values(last_daily_sync_at=at))

    def list_repos(self, member: str) -> list[RepoRecord]:
        """Return the stored repos of a member, sorted by name."""
        t = _table(m.Repo)
        rows = (
            self._c.execute(select(t).where(t.c.member == member).order_by(t.c.full_name))
            .mappings()
            .all()
        )
        return [RepoRecord(**dict(r)) for r in rows]

    def repo_counts(self, repo_id: int) -> RepoCounts:
        """Count runs, attempts, jobs, logs (by status) and commits of a repo."""
        wr, jb = _table(m.WorkflowRun), _table(m.Job)
        lg, cm = _table(m.Log), _table(m.Commit)
        runs, attempts = self._c.execute(
            select(func.count(wr.c.run_id.distinct()), func.count()).where(wr.c.repo_id == repo_id)
        ).one()
        job_on_run = and_(jb.c.run_id == wr.c.run_id, jb.c.attempt == wr.c.attempt)
        jobs = self._c.execute(
            select(func.count()).select_from(jb.join(wr, job_on_run)).where(wr.c.repo_id == repo_id)
        ).scalar_one()
        log_rows = self._c.execute(
            select(lg.c.status, func.count())
            .select_from(lg.join(jb, lg.c.job_id == jb.c.job_id).join(wr, job_on_run))
            .where(wr.c.repo_id == repo_id)
            .group_by(lg.c.status)
        ).all()
        commits = self._c.execute(
            select(func.count()).select_from(cm).where(cm.c.repo_id == repo_id)
        ).scalar_one()
        return RepoCounts(
            runs=int(runs),
            attempts=int(attempts),
            jobs=int(jobs),
            logs={str(status): int(n) for status, n in log_rows},
            commits=int(commits),
            logs_pending=int(
                self._c.execute(
                    select(func.count()).select_from(self._pending_query(repo_id).subquery())
                ).scalar_one()
            ),
            runs_skipped=int(
                self._c.execute(
                    select(func.count())
                    .select_from(_table(m.SkippedRun))
                    .where(_table(m.SkippedRun).c.repo_id == repo_id)
                ).scalar_one()
            ),
        )

    def run_days(self, repo_id: int) -> list[date]:
        """Return the distinct UTC calendar days on which a run was created, oldest first."""
        wr = _table(m.WorkflowRun)
        day = sa_cast(func.timezone(literal_column("'UTC'"), wr.c.created_at), Date).label("day")
        rows = self._c.execute(
            select(day)
            .where(wr.c.repo_id == repo_id, wr.c.created_at.is_not(None))
            .distinct()
            .order_by(day)
        ).all()
        return [r[0] for r in rows]
