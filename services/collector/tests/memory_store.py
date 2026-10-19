"""In-memory `Store` for tests that need no database; rolls back on exceptions like Postgres."""

from __future__ import annotations

import copy
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Any

from collector.store import (
    CommitFileRecord,
    CommitRecord,
    JobRecord,
    LogRecord,
    RepoRecord,
    RunRecord,
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
