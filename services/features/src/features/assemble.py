"""Turn neutral inputs into a `CommitContext`: the one place the three builders share.

The live, replay and pre-push builders only differ in where the inputs come from. Everything
after that (tagging, snapshots, workflow parsing, ordering) happens here, so the structures
are identical by construction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from features.file_tagger import RawFile, load_tagger, tag_files, tags_for
from features.workflow_parser import WorkflowParseError, parse_workflow
from shared.contracts import CommitContext, FileSnapshot, FileTag, RepoRef, WorkflowJob
from shared.file_tags import FileTagger
from shared.sanitise import sanitise

__all__ = [
    "ContextInputs",
    "WorkflowFile",
    "assemble_context",
    "normalise_status",
    "parse_repo",
    "scrub_text",
    "wants_content",
]

logger = logging.getLogger(__name__)

# DEFAULT: mirrors the collector's NON_SNAPSHOT_TAGS (source, tests, docs and unmatched files are
# visible in the patch, so only configuration files get a content snapshot). Services must not
# import each other, so the set is repeated here.
_NON_SNAPSHOT = frozenset({FileTag.TEST, FileTag.SOURCE, FileTag.DOCS, FileTag.OTHER})

_STATUS: dict[str, Literal["added", "modified", "removed", "renamed"]] = {
    "added": "added",
    "removed": "removed",
    "modified": "modified",
    "renamed": "renamed",
    # DEFAULT: GitHub's extra statuses fold into the four the contract has.
    "copied": "added",
    "changed": "modified",
    "unchanged": "modified",
}


@dataclass(frozen=True)
class WorkflowFile:
    """A workflow file at the head commit."""

    path: str
    content: str


@dataclass(frozen=True)
class ContextInputs:
    """Everything a builder has gathered. `files[*].content` is head content, where fetched."""

    repo: RepoRef
    head_sha: str
    base_sha: str | None
    event: Literal["push", "pull_request", "pre_push", "replay"]
    branch: str
    pr_number: int | None
    author_hash: str
    committed_at: datetime
    history_cutoff: datetime
    files: Sequence[RawFile]
    workflows: Sequence[WorkflowFile] = ()
    # True when the source already dropped a patch for size (the raw `patch_truncated` flag).
    source_truncated: bool = False
    is_synthetic: bool = False


def parse_repo(full_name: str) -> RepoRef:
    """Split `owner/name` into a RepoRef."""
    owner, sep, name = full_name.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"expected 'owner/name', got {full_name!r}")
    return RepoRef(owner=owner, name=name)


def normalise_status(status: str | None) -> Literal["added", "modified", "removed", "renamed"]:
    """Map a GitHub file status to the contract's four values."""
    return _STATUS.get(status or "", "modified")


def scrub_text(text: str) -> str:
    """Run the secret sanitiser and drop NUL characters (Postgres cannot hold them)."""
    return sanitise(text).text.replace("\x00", "")


def wants_content(path: str, tagger: FileTagger | None = None) -> bool:
    """True when a builder should fetch this changed file (a config file, not a lockfile)."""
    tags = set(tags_for(path, None, tagger or load_tagger()))
    return bool(tags - _NON_SNAPSHOT) and FileTag.LOCKFILE not in tags


def _snapshots(files: Iterable[RawFile], tagged: dict[str, list[FileTag]]) -> list[FileSnapshot]:
    out: list[FileSnapshot] = []
    for f in files:
        tags = tagged[f.path]
        if f.status == "removed" or f.content is None:
            continue
        if FileTag.LOCKFILE in tags:
            continue  # DEFAULT: lockfiles get no snapshot (by-reference storage is the collector's)
        kept = [t for t in tags if t not in _NON_SNAPSHOT]
        if not kept:
            continue
        # DEFAULT: head content only; raw tables hold snapshots at the head sha, and the patch
        # already shows the change, so base_content stays None in every builder.
        out.append(FileSnapshot(path=f.path, tag=kept[0], head_content=f.content, changed=True))
    return sorted(out, key=lambda s: s.path)


def _workflow_jobs(
    workflows: Iterable[WorkflowFile], scrub: Callable[[str], str]
) -> list[WorkflowJob]:
    jobs: list[WorkflowJob] = []
    seen: set[str] = set()
    for wf in sorted(workflows, key=lambda w: w.path):
        if wf.path in seen:
            continue
        seen.add(wf.path)
        try:
            jobs.extend(parse_workflow(wf.path, wf.content, scrub=scrub))
        except WorkflowParseError:
            # DEFAULT: an unreadable workflow file is skipped, not fatal; the patch still shows it.
            logger.warning("workflow file skipped", extra={"path": wf.path})
    return jobs


def _changed_workflows(
    files: Iterable[RawFile], tagged: dict[str, list[FileTag]]
) -> list[WorkflowFile]:
    """Workflow files the commit changed are always parsed, from their own head content."""
    return [
        WorkflowFile(f.path, f.content)
        for f in files
        if f.content is not None and f.status != "removed" and FileTag.WORKFLOW in tagged[f.path]
    ]


def assemble_context(
    inputs: ContextInputs,
    *,
    tagger: FileTagger | None = None,
    scrub: Callable[[str], str] = scrub_text,
) -> CommitContext:
    """Tag the files, build snapshots and workflow jobs, and return the CommitContext."""
    tagger = tagger or load_tagger()
    # Sorted by path so every builder returns the same order whatever its source gave.
    files = sorted(inputs.files, key=lambda f: f.path)
    tagged = tag_files(files, tagger)
    by_path = {f.path: f.tags for f in tagged.files}
    return CommitContext(
        repo=inputs.repo,
        head_sha=inputs.head_sha,
        base_sha=inputs.base_sha,
        event=inputs.event,
        branch=inputs.branch,
        pr_number=inputs.pr_number,
        author_hash=inputs.author_hash,
        committed_at=inputs.committed_at,
        history_cutoff=inputs.history_cutoff,
        changed_files=tagged.files,
        snapshots=_snapshots(files, by_path),
        workflow_jobs=_workflow_jobs(
            [*inputs.workflows, *_changed_workflows(files, by_path)], scrub
        ),
        is_synthetic=inputs.is_synthetic,
        truncated=tagged.truncated or inputs.source_truncated,
    )
