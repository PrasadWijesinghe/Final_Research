"""Live CommitContext builder: reads one commit from the GitHub API (ARCHITECTURE.md 3)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from features.assemble import (
    ContextInputs,
    WorkflowFile,
    assemble_context,
    normalise_status,
    parse_repo,
    scrub_text,
    wants_content,
)
from features.file_tagger import RawFile
from shared.contracts import CommitContext
from shared.file_tags import FileTagger
from shared.github.client import GitHubClient, GitHubNotFoundError
from shared.sanitise import hash_identity

__all__ = ["build_live_context"]

logger = logging.getLogger(__name__)

WORKFLOW_DIR = ".github/workflows"


def _get_all_files(client: GitHubClient, path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """GET a commit or compare response and follow its pages; return (first body, all files)."""
    response = client.get(path, {"per_page": 100})
    body: dict[str, Any] = response.json()
    files: list[dict[str, Any]] = list(body.get("files") or [])
    while next_url := response.links.get("next", {}).get("url"):
        response = client.get(next_url)
        files.extend(response.json().get("files") or [])
    return body, files


def _text(data: bytes | None) -> str | None:
    return None if data is None else data.decode("utf-8", errors="replace")


def build_live_context(
    client: GitHubClient,
    *,
    repo: str,
    head_sha: str,
    event: Literal["push", "pull_request"],
    branch: str,
    pr_number: int | None = None,
    base_sha: str | None = None,
    history_cutoff: datetime | None = None,
    workflow_paths: list[str] | None = None,
    tagger: FileTagger | None = None,
    hash_fn: Callable[[str], str] = hash_identity,
    scrub: Callable[[str], str] = scrub_text,
) -> CommitContext:
    """Build the context of `head_sha` from the GitHub API.

    `history_cutoff` is the push time (default: now). For a pull request with a `base_sha`
    the diff is the PR's (`compare`); otherwise it is the commit's own diff. With no
    `base_sha`, the commit's first parent is used. `workflow_paths` limits which workflow
    files are parsed; the default is every `.yml`/`.yaml` in `.github/workflows` at the commit.
    Workflow files the commit changed are always parsed too.
    """
    ref = parse_repo(repo)
    commit, commit_files = _get_all_files(client, f"/repos/{repo}/commits/{head_sha}")
    parents = [str(p["sha"]) for p in commit.get("parents") or []]
    # DEFAULT: base is the first parent; a PR without base_sha is diffed as one commit
    base = base_sha or (parents[0] if parents else None)
    if event == "pull_request" and base_sha:
        _, raw_files = _get_all_files(client, f"/repos/{repo}/compare/{base_sha}...{head_sha}")
    else:
        raw_files = commit_files

    info = commit.get("commit") or {}
    login = (commit.get("author") or {}).get("login")
    # DEFAULT: same as the collector: with no GitHub account, hash the commit's author email.
    identity = login or (info.get("author") or {}).get("email")
    if not identity:
        raise ValueError(f"commit {head_sha} has no author to hash")
    committed_at = datetime.fromisoformat(info["committer"]["date"]).astimezone(UTC)

    cache: dict[str, str | None] = {}

    def fetch(path: str) -> str | None:
        if path not in cache:
            text = _text(client.fetch_raw_file(repo, head_sha, path))
            cache[path] = scrub(text) if text is not None else None
        return cache[path]

    files: list[RawFile] = []
    for f in raw_files:
        path = str(f["filename"])
        status = normalise_status(f.get("status"))
        patch = f.get("patch")
        files.append(
            RawFile(
                path=path,
                status=status,
                additions=int(f.get("additions") or 0),
                deletions=int(f.get("deletions") or 0),
                patch=scrub(patch) if isinstance(patch, str) else None,
                previous_path=f.get("previous_filename"),
                content=fetch(path)
                if status != "removed" and wants_content(path, tagger)
                else None,
            )
        )

    workflows = [
        WorkflowFile(p, text)
        for p in (
            workflow_paths
            if workflow_paths is not None
            else _list_workflows(client, repo, head_sha)
        )
        if (text := fetch(p)) is not None
    ]
    return assemble_context(
        ContextInputs(
            repo=ref,
            head_sha=head_sha,
            base_sha=base,
            event=event,
            branch=branch,
            pr_number=pr_number,
            author_hash=hash_fn(str(identity)),
            committed_at=committed_at,
            # DEFAULT: cutoff is "now" when not given
            history_cutoff=history_cutoff or datetime.now(UTC),
            files=files,
            workflows=workflows,
        ),
        tagger=tagger,
        scrub=scrub,
    )


def _list_workflows(client: GitHubClient, repo: str, sha: str) -> list[str]:
    try:
        listing = client.get_json(f"/repos/{repo}/contents/{WORKFLOW_DIR}", {"ref": sha})
    except GitHubNotFoundError:
        return []
    return sorted(
        str(e["path"])
        for e in listing
        if e.get("type") == "file" and str(e.get("name", "")).endswith((".yml", ".yaml"))
    )
