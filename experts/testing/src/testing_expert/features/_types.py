"""Plain inputs shared by the feature groups (no database, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass

Value = int | float | str | None
FeatureDict = dict[str, Value]


@dataclass(frozen=True, slots=True)
class FileChange:
    """One changed file with its tags (from `config/file_tags.yaml`)."""

    path: str
    additions: int
    deletions: int
    tags: frozenset[str]
    patch: str | None = None  # only kept for package.json (version bumps)


@dataclass(frozen=True, slots=True)
class JobTarget:
    """The job attempt we compute features for, as stored in `raw`."""

    run_id: int
    attempt: int
    job_id: int
    repo_id: int
    cutoff: float  # history_cutoff = the run's created_at, as epoch seconds
    head_sha: str
    event: str | None
    branch: str | None
    default_branch: str | None
    runner_os: str | None
    workflow_path: str
    series_key: str  # job_key, else the job name without its "(matrix)" suffix
    matrix_size: int
    jobs_in_run: int
