"""Where the dataset builder reads stored CI history: the collector's `raw` tables and log files.

`SqlDatasetSource` reads Postgres; `InMemoryDatasetSource` holds plain lists for tests. Both only
read stored data. Nothing here talks to GitHub.
"""

from __future__ import annotations

import gzip
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine, and_, select
from sqlalchemy.orm import Session

from shared.db import models as m

from ..labels import StepFacts


@dataclass(frozen=True)
class RepoInfo:
    """One tracked repo (raw.repos)."""

    repo_id: int
    full_name: str
    default_branch: str | None = None
    licence: str | None = None


@dataclass(frozen=True)
class RunInfo:
    """One run attempt of a build-relevant workflow (raw.workflow_runs)."""

    run_id: int
    attempt: int
    head_sha: str
    created_at: datetime
    head_branch: str | None = None
    event: str | None = None
    status: str | None = None
    conclusion: str | None = None
    updated_at: datetime | None = None
    workflow_path: str | None = None
    pr_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class JobInfo:
    """One job of a run attempt, with its steps and where its log is stored."""

    job_id: int
    run_id: int
    attempt: int
    name: str | None = None
    job_key: str | None = None
    status: str | None = None
    conclusion: str | None = None
    steps: tuple[StepFacts, ...] = ()
    log_path: str | None = None
    log_status: str | None = None  # ok | expired | missing | None (never fetched)


class DatasetSource(Protocol):
    """Read-only access to stored runs, jobs, steps and logs."""

    def repos(self, member: str, only: Sequence[str] | None = None) -> list[RepoInfo]: ...

    def runs(
        self, repo: RepoInfo, start: datetime | None = None, end: datetime | None = None
    ) -> list[RunInfo]:
        """Attempts of the repo's build-relevant workflows created in [start, end)."""
        ...

    def jobs(self, runs: Sequence[RunInfo]) -> list[JobInfo]: ...

    def read_log(self, log_path: str) -> str | None: ...

    def commit_message(self, repo: RepoInfo, sha: str) -> str | None: ...


def _in_window(created: datetime, start: datetime | None, end: datetime | None) -> bool:
    return (start is None or created >= start) and (end is None or created < end)


@dataclass
class InMemoryDatasetSource:
    """A `DatasetSource` over lists, for tests and small tools."""

    repo_rows: list[tuple[str, RepoInfo]] = field(default_factory=list)  # (member, repo)
    run_rows: dict[str, list[RunInfo]] = field(default_factory=dict)  # full_name -> runs
    job_rows: list[JobInfo] = field(default_factory=list)
    logs: dict[str, str] = field(default_factory=dict)  # log_path -> text
    messages: dict[tuple[str, str], str] = field(default_factory=dict)

    def repos(self, member: str, only: Sequence[str] | None = None) -> list[RepoInfo]:
        """Repos of the member, optionally limited to `only`."""
        return [
            r for mem, r in self.repo_rows if mem == member and (not only or r.full_name in only)
        ]

    def runs(
        self, repo: RepoInfo, start: datetime | None = None, end: datetime | None = None
    ) -> list[RunInfo]:
        """Stored runs of the repo inside the window."""
        return [
            r for r in self.run_rows.get(repo.full_name, []) if _in_window(r.created_at, start, end)
        ]

    def jobs(self, runs: Sequence[RunInfo]) -> list[JobInfo]:
        """Jobs of the given run attempts."""
        keys = {(r.run_id, r.attempt) for r in runs}
        return [j for j in self.job_rows if (j.run_id, j.attempt) in keys]

    def read_log(self, log_path: str) -> str | None:
        """A stored log's text."""
        return self.logs.get(log_path)

    def commit_message(self, repo: RepoInfo, sha: str) -> str | None:
        """A stored commit message."""
        return self.messages.get((repo.full_name, sha))


class SqlDatasetSource:
    """A `DatasetSource` over the collector's Postgres tables and gzipped log files."""

    # DEFAULT: job ids are sent to Postgres in chunks of this size (keeps statements small).
    CHUNK = 5_000

    def __init__(self, engine: Engine, data_dir: Path) -> None:
        self._engine = engine
        self._data_dir = data_dir

    def repos(self, member: str, only: Sequence[str] | None = None) -> list[RepoInfo]:
        """Enabled repos registered by the member's collector."""
        with Session(self._engine) as s:
            q = select(m.Repo).where(m.Repo.member == member, m.Repo.enabled.is_(True))
            rows = s.scalars(q.order_by(m.Repo.full_name)).all()
            return [
                RepoInfo(r.id, r.full_name, r.default_branch, r.licence)
                for r in rows
                if not only or r.full_name in only
            ]

    def runs(
        self, repo: RepoInfo, start: datetime | None = None, end: datetime | None = None
    ) -> list[RunInfo]:
        """Attempts of the repo's workflows marked relevant by the collector."""
        q = (
            select(m.WorkflowRun)
            .join(
                m.Workflow,
                and_(
                    m.Workflow.repo_id == m.WorkflowRun.repo_id,
                    m.Workflow.path == m.WorkflowRun.workflow_path,
                ),
            )
            .where(m.WorkflowRun.repo_id == repo.repo_id, m.Workflow.relevant.is_(True))
        )
        if start is not None:
            q = q.where(m.WorkflowRun.created_at >= start)
        if end is not None:
            q = q.where(m.WorkflowRun.created_at < end)
        with Session(self._engine) as s:
            return [
                RunInfo(
                    run_id=r.run_id,
                    attempt=r.attempt,
                    head_sha=r.head_sha,
                    created_at=r.created_at,
                    head_branch=r.head_branch,
                    event=r.event,
                    status=r.status,
                    conclusion=r.conclusion,
                    updated_at=r.updated_at,
                    workflow_path=r.workflow_path,
                    pr_numbers=tuple(r.pr_numbers or ()),
                )
                for r in s.scalars(q).all()
                if r.created_at is not None
            ]

    def jobs(self, runs: Sequence[RunInfo]) -> list[JobInfo]:
        """Jobs, steps and log rows for the given run attempts."""
        wanted = {(r.run_id, r.attempt) for r in runs}
        run_ids = sorted({r.run_id for r in runs})
        out: list[JobInfo] = []
        with Session(self._engine) as s:
            for i in range(0, len(run_ids), self.CHUNK):
                chunk = run_ids[i : i + self.CHUNK]
                jobs = [
                    j
                    for j in s.scalars(select(m.Job).where(m.Job.run_id.in_(chunk))).all()
                    if (j.run_id, j.attempt) in wanted
                ]
                ids = [j.job_id for j in jobs]
                steps: dict[int, list[StepFacts]] = {}
                logs: dict[int, m.Log] = {}
                for k in range(0, len(ids), self.CHUNK):
                    part = ids[k : k + self.CHUNK]
                    for st in s.scalars(select(m.Step).where(m.Step.job_id.in_(part))).all():
                        steps.setdefault(st.job_id, []).append(
                            StepFacts(st.number, st.name, st.conclusion)
                        )
                    for lg in s.scalars(select(m.Log).where(m.Log.job_id.in_(part))).all():
                        logs[lg.job_id] = lg
                for j in jobs:
                    log_row = logs.get(j.job_id)
                    out.append(
                        JobInfo(
                            job_id=j.job_id,
                            run_id=j.run_id,
                            attempt=j.attempt,
                            name=j.name,
                            job_key=j.job_key,
                            status=j.status,
                            conclusion=j.conclusion,
                            steps=tuple(sorted(steps.get(j.job_id, []), key=lambda x: x.number)),
                            log_path=log_row.storage_path if log_row is not None else None,
                            log_status=log_row.status if log_row is not None else None,
                        )
                    )
        return out

    def read_log(self, log_path: str) -> str | None:
        """Decompress one stored log (already sanitised by the collector)."""
        try:
            data = gzip.decompress((self._data_dir / log_path).read_bytes())
        except (OSError, EOFError):
            return None
        return data.decode("utf-8", errors="replace")

    def commit_message(self, repo: RepoInfo, sha: str) -> str | None:
        """The stored (sanitised) commit message."""
        with Session(self._engine) as s:
            row = s.get(m.Commit, (repo.repo_id, sha))
            return row.message if row is not None else None
