"""Advisory Git pre-push client for the local Infra prediction demonstration."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.error import URLError
from urllib.request import Request, urlopen

from features.workflow_parser import WorkflowParseError, parse_workflow

from shared.contracts import ChangedFile, CommitContext, FileSnapshot, FileTag, RepoRef
from shared.file_tags import FileTagger
from shared.sanitise import sanitise

ZERO_SHA = "0" * 40
DEFAULT_API_URL = "http://127.0.0.1:8104/monitor/predict"
MAX_SNAPSHOT_CHARS = 200_000  # DEFAULT: enough for demo configs without sending large files.
PIPELINE_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str, cwd: Path, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return completed.stdout.rstrip("\n")


def _content_at(cwd: Path, revision: str, path: str) -> str | None:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        return None
    if len(completed.stdout) > MAX_SNAPSHOT_CHARS:
        raise ValueError(f"Configuration file exceeds snapshot limit: {path}")
    return sanitise(completed.stdout).text.replace("\x00", "")


def _repo_ref(cwd: Path, remote_url: str) -> RepoRef:
    cleaned = remote_url.removesuffix(".git").replace("\\", "/")
    if "://" in cleaned:
        pieces = cleaned.rstrip("/").split("/")
        if len(pieces) >= 2:
            return RepoRef(owner=pieces[-2], name=pieces[-1])
    if cleaned.startswith("git@") and ":" in cleaned:
        repo_path = cleaned.split(":", 1)[1]
        pieces = repo_path.rstrip("/").split("/")
        if len(pieces) >= 2:
            return RepoRef(owner=pieces[-2], name=pieces[-1])
    return RepoRef(owner="local-demo", name=cwd.name)


def _diff_rows(
    cwd: Path, base_sha: str, head_sha: str
) -> list[tuple[str, str, int, int, str | None]]:
    statuses: dict[str, str] = {}
    previous_paths: dict[str, str] = {}
    entries = iter(
        _git("diff", "--name-status", "-z", "-M", base_sha, head_sha, cwd=cwd).split("\0")
    )
    for status in entries:
        if not status:
            continue
        path = next(entries)
        if status.startswith("R"):
            new_path = next(entries)
            statuses[new_path] = "renamed"
            previous_paths[new_path] = path
        else:
            statuses[path] = {"A": "added", "D": "removed"}.get(status[0], "modified")

    counts: dict[str, tuple[int, int]] = {}
    entries = iter(_git("diff", "--numstat", "-z", "-M", base_sha, head_sha, cwd=cwd).split("\0"))
    for line in entries:
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        path = parts[-1]
        if not path:
            next(entries)  # Previous path in Git's NUL-delimited rename record.
            path = next(entries)
        additions = int(parts[0]) if parts[0].isdigit() else 0
        deletions = int(parts[1]) if parts[1].isdigit() else 0
        counts[path] = (additions, deletions)

    return [
        (path, status, *counts.get(path, (0, 0)), previous_paths.get(path))
        for path, status in statuses.items()
    ]


def build_context(
    *,
    cwd: Path,
    local_ref: str,
    head_sha: str,
    remote_sha: str,
    remote_url: str,
    event: Literal["push", "pre_push"] = "pre_push",
) -> CommitContext:
    """Build a real CommitContext from the commits Git is about to push."""
    if remote_sha == ZERO_SHA:
        # No remote branch exists yet: include the complete initial tree, even at a root commit.
        base_sha = None
        diff_base = subprocess.run(
            ["git", "hash-object", "-t", "tree", "--stdin"],
            input="",
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    else:
        base_sha = remote_sha
        diff_base = base_sha
    tagger = FileTagger.load(PIPELINE_ROOT / "config" / "file_tags.yaml")
    changed_files: list[ChangedFile] = []
    snapshots: list[FileSnapshot] = []
    workflow_jobs = []

    for path, status, additions, deletions, previous_path in _diff_rows(cwd, diff_base, head_sha):
        path_tags = tagger.tags_for(path, None)
        needs_content = {"workflow", "dockerfile", "deploy_config"}.intersection(path_tags)
        head_content = (
            _content_at(cwd, head_sha, path) if status != "removed" and needs_content else None
        )
        tags = sorted(tagger.tags_for(path, head_content))
        changed_files.append(
            ChangedFile(
                path=path,
                previous_path=previous_path,
                status=cast(Literal["added", "modified", "removed", "renamed"], status),
                additions=additions,
                deletions=deletions,
                tags=[FileTag(tag) for tag in tags],
            )
        )
        snapshot_tags = {"workflow", "dockerfile", "deploy_config"}.intersection(tags)
        for tag in sorted(snapshot_tags):
            snapshot = FileSnapshot(
                path=path,
                tag=FileTag(tag),
                base_content=(
                    _content_at(cwd, base_sha, previous_path or path) if base_sha else None
                ),
                head_content=head_content,
                changed=True,
            )
            snapshots.append(snapshot)
    # Include unchanged workflows: a source-only push still runs the existing CI configuration.
    workflow_snapshots = {item.path: item for item in snapshots if item.tag == FileTag.WORKFLOW}
    paths = _git("ls-tree", "-r", "--name-only", "-z", head_sha, cwd=cwd).split("\0")
    for path in paths:
        if not path or "workflow" not in tagger.tags_for(path, None):
            continue
        head_content = _content_at(cwd, head_sha, path)
        if path not in workflow_snapshots:
            snapshots.append(
                FileSnapshot(
                    path=path,
                    tag=FileTag.WORKFLOW,
                    head_content=head_content,
                    changed=False,
                )
            )
        if head_content:
            try:
                workflow_jobs.extend(parse_workflow(path, head_content))
            except WorkflowParseError as exc:
                print(f"PipelineIQ: workflow parser warning: {exc}", file=sys.stderr)

    branch = local_ref.removeprefix("refs/heads/")
    committed_at = datetime.fromisoformat(_git("show", "-s", "--format=%cI", head_sha, cwd=cwd))
    return CommitContext(
        repo=_repo_ref(cwd, remote_url),
        head_sha=head_sha,
        base_sha=base_sha,
        event=event,
        branch=branch,
        author_hash=hashlib.sha256(b"pipelineiq-local-demo").hexdigest(),
        committed_at=committed_at,
        history_cutoff=datetime.now(UTC),
        changed_files=changed_files,
        snapshots=snapshots,
        workflow_jobs=workflow_jobs,
    )


def request_prediction(context: CommitContext, api_url: str, timeout: float) -> dict[str, Any]:
    """Send one context to the local Infra API and return its first output."""
    body = context.model_dump_json().encode("utf-8")
    request = Request(api_url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is user-configured.
        outputs = json.loads(response.read())
    if not isinstance(outputs, list) or not outputs:
        raise ValueError("Infra API returned no prediction")
    result: dict[str, Any] = outputs[0]
    return result


def _push_lines(raw: str) -> list[tuple[str, str, str, str]]:
    parsed = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 4:
            parsed.append((parts[0], parts[1], parts[2], parts[3]))
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("remote_name", nargs="?", default="origin")
    parser.add_argument("remote_url", nargs="?", default="")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cwd = Path.cwd()
    remote_url = args.remote_url or _git("remote", "get-url", args.remote_name, cwd=cwd)
    push_lines = _push_lines(sys.stdin.read())
    if not push_lines:
        print("PipelineIQ: no branch update found; push continues.")
        return 0

    for local_ref, local_sha, _remote_ref, remote_sha in push_lines:
        if local_sha == ZERO_SHA:
            continue
        try:
            context = build_context(
                cwd=cwd,
                local_ref=local_ref,
                head_sha=local_sha,
                remote_sha=remote_sha,
                remote_url=remote_url,
            )
            prediction = request_prediction(context, args.api_url, args.timeout)
            probability = float(prediction["probability"])
            threshold = float(prediction["threshold"])
            verdict = "ABOVE alert threshold" if prediction["alert"] else "below alert threshold"
            print("\nPipelineIQ Infra pre-push prediction")
            print(f"  Repository: {context.repo.owner}/{context.repo.name}")
            print(f"  Branch:     {context.branch}")
            print(f"  Files:      {len(context.changed_files)}")
            print(f"  Fail risk:  {probability * 100:.2f}% ({verdict}: {threshold * 100:.2f}%)")
            print("  Dashboard:  open the localhost URL printed by the dashboard server.\n")
        except (
            OSError,
            ValueError,
            KeyError,
            URLError,
            subprocess.SubprocessError,
        ) as exc:
            print(f"PipelineIQ: prediction unavailable ({exc}); push continues.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
