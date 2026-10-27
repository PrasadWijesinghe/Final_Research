"""In-memory `Store` for tests that need no database; rolls back on exceptions like Postgres."""

from __future__ import annotations

import copy
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Any

from collector.policy import FAILED_CONCLUSIONS, LOG_CONCLUSIONS, PASSED_CONCLUSIONS
from collector.store import (
    CommitFileRecord,
    CommitRecord,
    JobRecord,
    LogRecord,
    PendingLog,
    RepoCounts,
    RepoQuality,
    RepoRecord,
    RunRecord,
    SkippedRunRecord,
    SnapshotRecord,
    StepRecord,
    SyncStateRecord,
    WorkflowRecord,
)

# Primary key columns per raw table, in the order used to sort dumps.
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "repos": ("id",),
    "sync_state": ("repo_id",),
    "workflows": ("repo_id", "path"),
    "workflow_runs": ("run_id", "attempt"),
    "jobs": ("job_id",),
    "steps": ("job_id", "number"),
    "commits": ("repo_id", "sha"),
    "commit_files": ("repo_id", "sha", "path"),
    "file_snapshots": ("repo_id", "sha", "path"),
    "logs": ("job_id",),
    "skipped_runs": ("repo_id", "run_id"),
}


class MemoryStore:
    """Dict-backed store with the same behaviour as SqlStore for everything the backfill uses."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple[Any, ...], Any]] = {t: {} for t in PRIMARY_KEYS}
        self._depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Copy the state on entry and restore it if the block raises (outermost only)."""
        outer = self._depth == 0
        saved = copy.deepcopy(self.tables) if outer else None
        self._depth += 1
        try:
            yield
        except BaseException:
            if saved is not None:
                self.tables = saved
            raise
        finally:
            self._depth -= 1

    def _put(self, table: str, record: Any) -> None:
        values = asdict(record)
        key = tuple(values[c] for c in PRIMARY_KEYS[table])
        self.tables[table][key] = record

    def dump(self) -> dict[str, list[dict[str, Any]]]:
        """Every table as a list of column dicts sorted by primary key."""
        return {
            t: [asdict(r) for _, r in sorted(rows.items(), key=lambda kv: repr(kv[0]))]
            for t, rows in self.tables.items()
        }

    # ---- Store ------------------------------------------------------------------------------

    def upsert_repo(self, repo: RepoRecord) -> None:
        self._put("repos", repo)

    def ensure_sync_state(self, repo_id: int) -> None:
        self.tables["sync_state"].setdefault((repo_id,), SyncStateRecord(repo_id))

    def get_sync_state(self, repo_id: int) -> SyncStateRecord | None:
        found: SyncStateRecord | None = self.tables["sync_state"].get((repo_id,))
        return found

    def set_cursor(self, repo_id: int, cursor: str | None) -> None:
        state = self.tables["sync_state"][(repo_id,)]
        self.tables["sync_state"][(repo_id,)] = SyncStateRecord(
            repo_id, state.backfill_done_at, cursor, state.last_daily_sync_at
        )

    def finish_backfill(self, repo_id: int, done_at: datetime, daily_at: datetime) -> None:
        state = self.tables["sync_state"][(repo_id,)]
        self.tables["sync_state"][(repo_id,)] = SyncStateRecord(
            repo_id,
            state.backfill_done_at or done_at,
            None,
            state.last_daily_sync_at or daily_at,
        )

    def get_workflows(self, repo_id: int) -> dict[str, WorkflowRecord]:
        return {w.path: w for w in self.tables["workflows"].values() if w.repo_id == repo_id}

    def upsert_workflow(self, workflow: WorkflowRecord) -> None:
        self._put("workflows", workflow)

    def attempt_done(self, repo_id: int, run_id: int, attempt: int) -> bool:
        run = self.tables["workflow_runs"].get((run_id, attempt))
        return bool(run and run.repo_id == repo_id and run.status == "completed")

    def upsert_run(self, run: RunRecord) -> None:
        self._put("workflow_runs", run)

    def upsert_job(self, job: JobRecord) -> None:
        self._put("jobs", job)

    def upsert_steps(self, steps: Sequence[StepRecord]) -> None:
        for step in steps:
            self._put("steps", step)

    def has_commit(self, repo_id: int, sha: str) -> bool:
        return (repo_id, sha) in self.tables["commits"]

    def upsert_commit(self, commit: CommitRecord, files: Sequence[CommitFileRecord]) -> None:
        self._put("commits", commit)
        for f in files:
            self._put("commit_files", f)

    def commit_files(self, repo_id: int, sha: str) -> list[tuple[str, str | None]]:
        rows = [
            (f.path, f.status)
            for f in self.tables["commit_files"].values()
            if f.repo_id == repo_id and f.sha == sha
        ]
        return sorted(rows)

    def get_snapshot(self, repo_id: int, sha: str, path: str) -> SnapshotRecord | None:
        found: SnapshotRecord | None = self.tables["file_snapshots"].get((repo_id, sha, path))
        return found

    def upsert_snapshot(self, snapshot: SnapshotRecord) -> None:
        self._put("file_snapshots", snapshot)

    def has_log(self, job_id: int) -> bool:
        return (job_id,) in self.tables["logs"]

    def upsert_log(self, log: LogRecord) -> None:
        self._put("logs", log)

    def _pending(self, repo_id: int, failed_runs_only: bool = False) -> list[tuple[Any, JobRecord]]:
        runs = {k: r for k, r in self.tables["workflow_runs"].items() if r.repo_id == repo_id}
        failed_run_ids = {
            j.run_id for j in self.tables["jobs"].values() if j.conclusion in FAILED_CONCLUSIONS
        }
        out = []
        for job in self.tables["jobs"].values():
            run = runs.get((job.run_id, job.attempt))
            if (
                run is not None
                and job.status == "completed"
                and job.conclusion in LOG_CONCLUSIONS
                and (job.job_id,) not in self.tables["logs"]
                and not (
                    failed_runs_only
                    and job.conclusion in PASSED_CONCLUSIONS
                    and job.run_id not in failed_run_ids
                )
            ):
                out.append((run, job))
        return out

    def pending_logs(
        self, repo_id: int, limit: int | None, *, failed_runs_only: bool = False
    ) -> list[PendingLog]:
        found = sorted(
            self._pending(repo_id, failed_runs_only),
            key=lambda rj: (
                rj[0].created_at is not None,
                rj[0].created_at or datetime.min.replace(tzinfo=UTC),
                rj[1].run_id,
                rj[1].attempt,
                rj[1].job_id,
            ),
            reverse=True,
        )
        rows = [PendingLog(j.job_id, j.run_id, j.attempt) for _, j in found]
        return rows if limit is None else rows[:limit]

    def job_names(self, run_id: int, attempt: int) -> list[tuple[int, str | None]]:
        rows = [
            (j.job_id, j.name)
            for j in self.tables["jobs"].values()
            if j.run_id == run_id and j.attempt == attempt
        ]
        return sorted(rows, key=lambda r: r[0])

    def add_skipped_run(self, skipped: SkippedRunRecord) -> None:
        self._put("skipped_runs", skipped)

    def clear_skipped_run(self, repo_id: int, run_id: int) -> None:
        self.tables["skipped_runs"].pop((repo_id, run_id), None)

    def skipped_runs(self, repo_id: int) -> list[SkippedRunRecord]:
        rows: list[SkippedRunRecord] = [
            r for r in self.tables["skipped_runs"].values() if r.repo_id == repo_id
        ]
        return sorted(rows, key=lambda r: r.run_id, reverse=True)

    def set_last_daily_sync(self, repo_id: int, at: datetime) -> None:
        state = self.tables["sync_state"][(repo_id,)]
        self.tables["sync_state"][(repo_id,)] = SyncStateRecord(
            repo_id, state.backfill_done_at, state.backfill_cursor, at
        )

    def list_repos(self, member: str) -> list[RepoRecord]:
        repos: list[RepoRecord] = [r for r in self.tables["repos"].values() if r.member == member]
        return sorted(repos, key=lambda r: r.full_name)

    def repo_counts(self, repo_id: int) -> RepoCounts:
        runs = [r for r in self.tables["workflow_runs"].values() if r.repo_id == repo_id]
        keys = {(r.run_id, r.attempt) for r in runs}
        jobs = [j for j in self.tables["jobs"].values() if (j.run_id, j.attempt) in keys]
        job_ids = {j.job_id for j in jobs}
        logs: dict[str, int] = {}
        for log in self.tables["logs"].values():
            if log.job_id in job_ids:
                logs[log.status] = logs.get(log.status, 0) + 1
        commits = [c for c in self.tables["commits"].values() if c.repo_id == repo_id]
        return RepoCounts(
            runs=len({r.run_id for r in runs}),
            attempts=len(runs),
            jobs=len(jobs),
            logs=logs,
            commits=len(commits),
            logs_pending=len(self._pending(repo_id)),
            runs_skipped=len(self.skipped_runs(repo_id)),
        )

    def run_days(self, repo_id: int) -> list[date]:
        return sorted(
            {
                r.created_at.astimezone(UTC).date()
                for r in self.tables["workflow_runs"].values()
                if r.repo_id == repo_id and r.created_at is not None
            }
        )

    def repo_quality(self, repo_id: int) -> RepoQuality:
        runs = [r for r in self.tables["workflow_runs"].values() if r.repo_id == repo_id]
        keys = {(r.run_id, r.attempt) for r in runs}
        jobs = [j for j in self.tables["jobs"].values() if (j.run_id, j.attempt) in keys]
        by_conclusion: dict[str | None, int] = {}
        for job in jobs:
            by_conclusion[job.conclusion] = by_conclusion.get(job.conclusion, 0) + 1
        job_ids = {j.job_id for j in jobs}
        attempts: dict[int, int] = {}
        for r in runs:
            attempts[r.run_id] = attempts.get(r.run_id, 0) + 1
        created = [r.created_at for r in runs if r.created_at is not None]
        return RepoQuality(
            jobs_by_conclusion=by_conclusion,
            runs_with_failed_job=len(
                {j.run_id for j in jobs if j.conclusion in FAILED_CONCLUSIONS}
            ),
            runs_with_reruns=sum(1 for n in attempts.values() if n > 1),
            redactions=sum(
                log.redactions or 0 for log in self.tables["logs"].values() if log.job_id in job_ids
            ),
            first_run_at=min(created) if created else None,
            last_run_at=max(created) if created else None,
        )
