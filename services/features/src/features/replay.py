"""Replay CommitContext builder: rebuilds a context from the stored raw tables, never from GitHub.

`history_cutoff` is the run's `created_at`. Reads go through the `RawHistory` interface, so unit
tests use `InMemoryHistory` and only the SQL version needs a database.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from features.assemble import (
    ContextInputs,
    WorkflowFile,
    assemble_context,
    normalise_status,
    parse_repo,
    wants_content,
)
from features.file_tagger import RawFile
from shared.contracts import CommitContext
from shared.db import models as m
from shared.file_tags import FileTagger

__all__ = [
    "CommitFileRow",
    "CommitRow",
    "InMemoryHistory",
    "RawHistory",
    "ReplayDataError",
    "RunRow",
    "SnapshotRow",
    "SqlHistory",
    "build_replay_context",
]

logger = logging.getLogger(__name__)


class ReplayDataError(LookupError):
    """The raw tables do not hold what a replay context needs (missing run, commit or field)."""


@dataclass(frozen=True)
class RunRow:
    """The columns of `raw.workflow_runs` a replay needs."""

    run_id: int
    attempt: int
    head_sha: str
    created_at: datetime
    workflow_path: str | None = None
    head_branch: str | None = None
    actor_hash: str | None = None
    pr_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class CommitRow:
    """The columns of `raw.commits` a replay needs."""

    sha: str
    committed_at: datetime | None
    author_hash: str | None = None
    parent_shas: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommitFileRow:
    """One `raw.commit_files` row."""

    path: str
    status: str | None
    additions: int | None = 0
    deletions: int | None = 0
    patch: str | None = None
    patch_truncated: bool = False
    previous_path: str | None = None


@dataclass(frozen=True)
class SnapshotRow:
    """One `raw.file_snapshots` row (`content` is None for lockfiles stored by reference)."""

    path: str
    content: str | None = None
    storage_path: str | None = None


class RawHistory(Protocol):
    """Read-only access to the raw tables, by repo `owner/name`."""

    def get_run(self, repo: str, run_id: int, attempt: int) -> RunRow | None: ...

    def runs_for_sha(self, repo: str, sha: str, before: datetime) -> list[RunRow]:
        """Runs of the commit created strictly before `before`."""
        ...

    def get_commit(self, repo: str, sha: str) -> CommitRow | None: ...

    def commit_files(self, repo: str, sha: str) -> list[CommitFileRow]: ...

    def get_snapshot(self, repo: str, sha: str, path: str) -> SnapshotRow | None: ...


@dataclass
class InMemoryHistory:
    """A `RawHistory` over plain lists, for unit tests and small tools."""

    runs: list[tuple[str, RunRow]] = field(default_factory=list)
    commits: dict[tuple[str, str], CommitRow] = field(default_factory=dict)
    files: dict[tuple[str, str], list[CommitFileRow]] = field(default_factory=dict)
    snapshots: dict[tuple[str, str, str], SnapshotRow] = field(default_factory=dict)

    def get_run(self, repo: str, run_id: int, attempt: int) -> RunRow | None:
        """Return one run attempt."""
        for r, run in self.runs:
            if r == repo and run.run_id == run_id and run.attempt == attempt:
                return run
        return None

    def runs_for_sha(self, repo: str, sha: str, before: datetime) -> list[RunRow]:
        """Runs of the commit created strictly before `before`."""
        return [
            r for rp, r in self.runs if rp == repo and r.head_sha == sha and r.created_at < before
        ]

    def get_commit(self, repo: str, sha: str) -> CommitRow | None:
        """Return a commit row."""
        return self.commits.get((repo, sha))

    def commit_files(self, repo: str, sha: str) -> list[CommitFileRow]:
        """Return the commit's files."""
        return list(self.files.get((repo, sha), []))

    def get_snapshot(self, repo: str, sha: str, path: str) -> SnapshotRow | None:
        """Return a file snapshot."""
        return self.snapshots.get((repo, sha, path))


class SqlHistory:
    """A `RawHistory` over the `raw` schema (Postgres)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _repo_id(self, s: Session, repo: str) -> int | None:
        return s.scalar(select(m.Repo.id).where(m.Repo.full_name == repo))

    @staticmethod
    def _run(r: m.WorkflowRun) -> RunRow:
        assert r.created_at is not None
        return RunRow(
            run_id=r.run_id,
            attempt=r.attempt,
            head_sha=r.head_sha,
            created_at=r.created_at,
            workflow_path=r.workflow_path,
            head_branch=r.head_branch,
            actor_hash=r.actor_hash,
            pr_numbers=tuple(r.pr_numbers or ()),
        )

    def get_run(self, repo: str, run_id: int, attempt: int) -> RunRow | None:
        """Return one run attempt."""
        with Session(self._engine) as s:
            repo_id = self._repo_id(s, repo)
            row = s.get(m.WorkflowRun, (run_id, attempt))
            if repo_id is None or row is None or row.repo_id != repo_id or row.created_at is None:
                return None
            return self._run(row)

    def runs_for_sha(self, repo: str, sha: str, before: datetime) -> list[RunRow]:
        """Runs of the commit created strictly before `before`."""
        with Session(self._engine) as s:
            repo_id = self._repo_id(s, repo)
            if repo_id is None:
                return []
            rows = s.scalars(
                select(m.WorkflowRun).where(
                    m.WorkflowRun.repo_id == repo_id,
                    m.WorkflowRun.head_sha == sha,
                    m.WorkflowRun.created_at < before,
                )
            ).all()
            return [self._run(r) for r in rows]

    def get_commit(self, repo: str, sha: str) -> CommitRow | None:
        """Return a commit row."""
        with Session(self._engine) as s:
            repo_id = self._repo_id(s, repo)
            row = s.get(m.Commit, (repo_id, sha)) if repo_id is not None else None
            if row is None:
                return None
            return CommitRow(
                sha=row.sha,
                committed_at=row.committed_at,
                author_hash=row.author_hash,
                parent_shas=tuple(row.parent_shas or ()),
            )

    def commit_files(self, repo: str, sha: str) -> list[CommitFileRow]:
        """Return the commit's files."""
        with Session(self._engine) as s:
            repo_id = self._repo_id(s, repo)
            if repo_id is None:
                return []
            rows = s.scalars(
                select(m.CommitFile)
                .where(m.CommitFile.repo_id == repo_id, m.CommitFile.sha == sha)
                .order_by(m.CommitFile.path)
            ).all()
            return [
                CommitFileRow(
                    path=r.path,
                    status=r.status,
                    additions=r.additions,
                    deletions=r.deletions,
                    patch=r.patch,
                    patch_truncated=r.patch_truncated,
                    previous_path=r.previous_path,
                )
                for r in rows
            ]

    def get_snapshot(self, repo: str, sha: str, path: str) -> SnapshotRow | None:
        """Return a file snapshot."""
        with Session(self._engine) as s:
            repo_id = self._repo_id(s, repo)
            row = s.get(m.FileSnapshot, (repo_id, sha, path)) if repo_id is not None else None
            if row is None:
                return None
            return SnapshotRow(path=row.path, content=row.content, storage_path=row.storage_path)


def _snapshot_text(history: RawHistory, repo: str, sha: str, path: str) -> str | None:
    row = history.get_snapshot(repo, sha, path)
    return row.content if row else None


def build_replay_context(
    history: RawHistory,
    *,
    repo: str,
    run_id: int,
    attempt: int,
    workflow_paths: Iterable[str] | None = None,
    tagger: FileTagger | None = None,
) -> CommitContext:
    """Rebuild the context of a stored run's commit, with `history_cutoff` = the run's `created_at`.

    Only raw rows are read; GitHub is never contacted. `workflow_paths` limits which workflow
    files are parsed. By default they are the workflow files of this commit's runs created before
    the cutoff (plus this run's). Workflow files the commit changed are always parsed too.
    """
    ref = parse_repo(repo)
    run = history.get_run(repo, run_id, attempt)
    if run is None:
        raise ReplayDataError(f"{repo}: run {run_id} attempt {attempt} is not in the raw tables")
    cutoff = run.created_at
    sha = run.head_sha
    commit = history.get_commit(repo, sha)
    if commit is None or commit.committed_at is None:
        raise ReplayDataError(f"{repo}@{sha}: commit is not in the raw tables")
    # DEFAULT: fall back to the run's actor when the commit row has no author hash.
    author_hash = commit.author_hash or run.actor_hash
    if not author_hash:
        raise ReplayDataError(f"{repo}@{sha}: no author hash stored")
    if not run.head_branch:
        raise ReplayDataError(f"{repo}: run {run_id} has no head_branch")

    rows = history.commit_files(repo, sha)
    files = [
        RawFile(
            path=r.path,
            status=normalise_status(r.status),
            additions=r.additions or 0,
            deletions=r.deletions or 0,
            patch=r.patch,
            previous_path=r.previous_path,
            content=(
                _snapshot_text(history, repo, sha, r.path)
                if normalise_status(r.status) != "removed" and wants_content(r.path, tagger)
                else None
            ),
        )
        for r in rows
    ]

    if workflow_paths is None:
        found = {
            r.workflow_path for r in history.runs_for_sha(repo, sha, cutoff) if r.workflow_path
        }
        if run.workflow_path:
            found.add(run.workflow_path)
        paths = sorted(found)
    else:
        paths = sorted(set(workflow_paths))
    workflows = [
        WorkflowFile(p, text) for p in paths if (text := _snapshot_text(history, repo, sha, p))
    ]

    return assemble_context(
        ContextInputs(
            repo=ref,
            head_sha=sha,
            base_sha=commit.parent_shas[0] if commit.parent_shas else None,
            event="replay",
            branch=run.head_branch,
            # DEFAULT: first PR number of the run
            pr_number=run.pr_numbers[0] if run.pr_numbers else None,
            author_hash=author_hash,
            committed_at=commit.committed_at,
            history_cutoff=cutoff,
            files=files,
            workflows=workflows,
            source_truncated=any(r.patch_truncated for r in rows),
        ),
        tagger=tagger,
    )
