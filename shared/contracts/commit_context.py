"""Commit context: produced by services/features, read by all four experts before a run.

Design: the shared service tags every changed file once (FileTag), and attaches the
before/after contents of the config files each expert needs. Each expert then filters
by tag instead of re-fetching from GitHub.

What each expert reads (from the four proposals):
  dependency: PACKAGE_MANIFEST, LOCKFILE, node_versions
  build:      SOURCE, BUILD_CONFIG, LINT_CONFIG, BUNDLER_CONFIG, package build scripts, node_versions
  test:       SOURCE, TEST, TEST_CONFIG, workflow_jobs (to find the test job)
  infra:      WORKFLOW, DOCKERFILE, DEPLOY_CONFIG, workflow_jobs (runner, permissions)

History (past runs, fail rates, flip rates) is NOT embedded. Experts query the shared
database themselves, using only records created before `history_cutoff`. This one rule
prevents look-ahead leakage for all four models.
"""

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .common import CommitSha, ContractModel, Part, PersonHash, RepoRef, StorageRef


class FileTag(str, Enum):
    SOURCE = "source"  # .js .jsx .ts .tsx .mjs .cjs outside test folders
    TEST = "test"  # *.test.*, *.spec.*, __tests__/, test/
    TEST_CONFIG = "test_config"  # jest.config.*, vitest.config.*, .mocharc.*
    PACKAGE_MANIFEST = "package_manifest"  # package.json
    LOCKFILE = "lockfile"  # package-lock.json, yarn.lock, pnpm-lock.yaml
    BUILD_CONFIG = "build_config"  # tsconfig*.json, babel config
    LINT_CONFIG = "lint_config"  # eslint config, .prettierrc
    BUNDLER_CONFIG = "bundler_config"  # webpack.config.*, vite.config.*, rollup.config.*
    WORKFLOW = "workflow"  # .github/workflows/*.yml
    DOCKERFILE = "dockerfile"  # Dockerfile*, docker-compose*.yml
    DEPLOY_CONFIG = "deploy_config"  # vercel.json, fly.toml, k8s manifests, etc.
    DOCS = "docs"
    OTHER = "other"


class ChangedFile(Part):
    path: str
    previous_path: str | None = None  # set when renamed
    status: Literal["added", "modified", "removed", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    # Unified diff for this file. None when binary or larger than the size limit.
    patch: str | None = None
    tags: list[FileTag] = Field(min_length=1)


class FileSnapshot(Part):
    """Before/after contents of a config file. Large files (lockfiles) use refs instead."""

    path: str
    tag: FileTag
    base_content: str | None = None  # None if the file did not exist before
    head_content: str | None = None  # None if the file was deleted
    base_ref: StorageRef | None = None  # used instead of base_content for large files
    head_ref: StorageRef | None = None
    changed: bool  # False = included for reference only (e.g. unchanged tsconfig)

    @model_validator(mode="after")
    def _one_source_per_side(self) -> "FileSnapshot":
        if self.base_content is not None and self.base_ref is not None:
            raise ValueError("use base_content or base_ref, not both")
        if self.head_content is not None and self.head_ref is not None:
            raise ValueError("use head_content or head_ref, not both")
        return self


class WorkflowJob(Part):
    """One job parsed from a workflow file at head_sha."""

    workflow_path: str  # e.g. ".github/workflows/ci.yml"
    job_key: str  # the YAML key, stable across runs (job names can be templated)
    job_name: str | None = None
    runs_on: list[str] = Field(default_factory=list)
    node_versions: list[str] = Field(default_factory=list)  # from setup-node / matrix
    permissions: dict[str, str] = Field(default_factory=dict)
    uses_container: bool = False
    commands: list[str] = Field(default_factory=list)  # `run:` lines, sanitised


class CommitContext(ContractModel):
    context_id: UUID = Field(default_factory=uuid4)
    repo: RepoRef
    head_sha: CommitSha
    base_sha: CommitSha | None = None  # parent commit, or PR base
    # push / pull_request: live, from the GitHub App.
    # pre_push: from the developer's local diff via the pre-push hook. The commit is not
    #           on GitHub yet, so head_sha is the local SHA and base_sha the remote tip.
    # replay: rebuilt from stored history for training or evaluation.
    event: Literal["push", "pull_request", "pre_push", "replay"]
    branch: str
    pr_number: int | None = None
    author_hash: PersonHash  # salted hash, never the raw login
    committed_at: datetime  # author-set, so unreliable for ordering; don't use as the cutoff
    # Experts may only use records created before this. Live: the push time.
    # Replay: the run's created_at. Use this, not committed_at, for temporal splits.
    history_cutoff: datetime
    changed_files: list[ChangedFile]
    snapshots: list[FileSnapshot] = Field(default_factory=list)
    workflow_jobs: list[WorkflowJob] = Field(default_factory=list)
    is_synthetic: bool = False  # True for fault-injected commits; report them separately
    truncated: bool = False  # True if the diff exceeded limits and files were dropped

    @model_validator(mode="after")
    def _check(self) -> "CommitContext":
        if self.event == "pull_request" and self.pr_number is None:
            raise ValueError("pull_request events need pr_number")
        if self.event == "pre_push" and self.pr_number is not None:
            raise ValueError("pre_push contexts have no pr_number")
        return self

    def files_with(self, *tags: FileTag) -> list[ChangedFile]:
        """Changed files carrying any of the given tags."""
        wanted = set(tags)
        return [f for f in self.changed_files if wanted.intersection(f.tags)]
