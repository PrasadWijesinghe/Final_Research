"""Pre-push CommitContext builder: from the local diff payload the git hook sends.

The payload is defined here (not in `shared/contracts/`) because only the hook and the backend
API use it. P2.6's `POST /predict` accepts a `PrePushPayload` and calls `build_pre_push_context`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from features.assemble import (
    ContextInputs,
    WorkflowFile,
    assemble_context,
    parse_repo,
    scrub_text,
)
from features.file_tagger import RawFile
from shared.contracts import CommitContext
from shared.file_tags import FileTagger

__all__ = ["PrePushFile", "PrePushPayload", "PrePushWorkflow", "build_pre_push_context"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrePushFile(_Payload):
    """One file changed by the commits being pushed."""

    path: str
    previous_path: str | None = None
    status: Literal["added", "modified", "removed", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    patch: str | None = None  # unified diff; None for binary files
    # Head content. The hook sends it only for config files (package.json, tsconfig, workflows...);
    # anything else is ignored.
    content: str | None = None


class PrePushWorkflow(_Payload):
    """A workflow file at the local head that the push did not change."""

    path: str
    content: str


class PrePushPayload(_Payload):
    """What the pre-push hook sends: the commit's local diff and config files."""

    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")  # "owner/name"
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")  # the local commit being pushed
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")  # remote tip
    branch: str
    committed_at: datetime
    files: list[PrePushFile]
    # Workflow files the push did not change; changed ones travel in `files` with `content`.
    workflows: list[PrePushWorkflow] = Field(default_factory=list)
    # No author field on purpose: the API knows the user from the API key and passes the hash.


def build_pre_push_context(
    payload: PrePushPayload,
    *,
    author_hash: str,
    history_cutoff: datetime | None = None,
    tagger: FileTagger | None = None,
    scrub: Callable[[str], str] = scrub_text,
) -> CommitContext:
    """Build the context of an unpushed commit. `history_cutoff` defaults to now."""
    files = [
        RawFile(
            path=f.path,
            status=f.status,
            additions=f.additions,
            deletions=f.deletions,
            patch=scrub(f.patch) if f.patch is not None else None,
            previous_path=f.previous_path,
            content=scrub(f.content) if f.content is not None and f.status != "removed" else None,
        )
        for f in payload.files
    ]
    return assemble_context(
        ContextInputs(
            repo=parse_repo(payload.repo),
            head_sha=payload.head_sha,
            base_sha=payload.base_sha,
            event="pre_push",
            branch=payload.branch,
            pr_number=None,
            author_hash=author_hash,
            committed_at=payload.committed_at,
            # DEFAULT: cutoff is "now" when not given
            history_cutoff=history_cutoff or datetime.now(UTC),
            files=files,
            workflows=[WorkflowFile(w.path, scrub(w.content)) for w in payload.workflows],
        ),
        tagger=tagger,
        scrub=scrub,
    )
