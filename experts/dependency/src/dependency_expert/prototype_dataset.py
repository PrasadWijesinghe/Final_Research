"""Collect a small, experimental dataset using the configured Dependency repositories."""

import argparse
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from dependency_expert.features.prototype import write_candidate_datasets
from shared.github import GitHubClient

logger = logging.getLogger(__name__)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG = REPOSITORY_ROOT / "config/repos/dependency.yaml"
DEFAULT_GROUPS = REPOSITORY_ROOT / "config/dependency_repository_groups.yaml"
DEFAULT_DATA = REPOSITORY_ROOT / "data/datasets/dependency/prototype/dependency_runs_dataset.json"
DEFAULT_FEATURE_CSV = REPOSITORY_ROOT / "data/datasets/dependency/prototype/dependency_features.csv"
# DEFAULT: the documented collection horizon is the last 90 days.
DEFAULT_DAYS = 90
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def validate_repository_names(repositories: list[str], source: str) -> list[str]:
    """Validate and de-duplicate GitHub owner/repo names while preserving order."""
    invalid = [repo for repo in repositories if not _REPOSITORY_NAME.fullmatch(repo)]
    if invalid:
        raise ValueError(f"{source} contains invalid owner/repo name(s): {', '.join(invalid)}")
    return list(dict.fromkeys(repositories))


def load_repositories(config_path: Path) -> list[str]:
    """Load enabled repo names from the member config, rejecting malformed entries."""
    with config_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    if not isinstance(config, dict) or config.get("member") != "dependency":
        raise ValueError(f"{config_path} must be the Dependency repo configuration")
    repos = config.get("repos")
    if not isinstance(repos, list):
        raise ValueError(f"{config_path} field repos must be a list")

    names: list[str] = []
    for index, repo in enumerate(repos):
        if not isinstance(repo, dict) or not isinstance(repo.get("full_name"), str):
            raise ValueError(f"{config_path} repo entry {index + 1} needs a full_name")
        if repo.get("enabled", True):
            names.append(repo["full_name"])
    return validate_repository_names(names, str(config_path))


def load_repository_group(groups_path: Path, group: str) -> list[str]:
    """Load one named research candidate group and validate its repository names."""
    with groups_path.open(encoding="utf-8") as groups_file:
        config = yaml.safe_load(groups_file)
    groups = config.get("groups") if isinstance(config, dict) else None
    if not isinstance(groups, dict):
        raise ValueError(f"{groups_path} must contain a groups mapping")
    repositories = groups.get(group)
    if not isinstance(repositories, list) or not repositories:
        available = ", ".join(sorted(groups))
        raise ValueError(f"unknown or empty repository group {group!r}; available: {available}")
    if any(not isinstance(repo, str) for repo in repositories):
        raise ValueError(f"{groups_path} group {group!r} must contain owner/repo strings")
    return validate_repository_names(repositories, f"{groups_path} group {group!r}")


def dependency_files_changed(client: GitHubClient, repo: str, sha: str) -> list[dict[str, str]]:
    """Return changed manifest and lockfile names for one commit; do not store patch contents."""
    commit = client.get_json(f"/repos/{repo}/commits/{sha}")
    files = commit.get("files", []) if isinstance(commit, dict) else []
    dependency_files: list[dict[str, str]] = []
    wanted = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if isinstance(filename, str) and Path(filename).name in wanted:
            dependency_files.append(
                {"filename": filename, "status": str(item.get("status", "modified"))}
            )
    return dependency_files


def read_existing_data(data_path: Path) -> tuple[list[dict[str, Any]], set[tuple[str, str, int]]]:
    """Read existing records and keys, refusing to overwrite malformed local data."""
    if not data_path.exists():
        return [], set()
    with data_path.open(encoding="utf-8") as input_file:
        dataset = json.load(input_file)
    if not isinstance(dataset, list) or any(not isinstance(row, dict) for row in dataset):
        raise ValueError(f"{data_path} must contain a JSON array of objects")
    keys = {
        (row["repository"], str(row["run_id"]), int(row.get("attempt", 1)))
        for row in dataset
        if isinstance(row.get("repository"), str) and row.get("run_id") is not None
    }
    return dataset, keys


def collect_dataset(
    client: GitHubClient,
    repositories: list[str],
    data_path: Path,
    *,
    days: int = DEFAULT_DAYS,
) -> int:
    """Append unseen run records for configured repos and return the number added."""
    if days <= 0:
        raise ValueError("days must be positive")
    dataset, seen = read_existing_data(data_path)
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    commit_cache: dict[tuple[str, str], list[dict[str, str]]] = {}
    added = 0

    for repo in repositories:
        for run in client.iter_workflow_runs(repo, start, end):
            run_id = run.get("id")
            head_sha = run.get("head_sha")
            attempt = int(run.get("run_attempt", 1))
            if run_id is None or not isinstance(head_sha, str):
                continue
            key = (repo, str(run_id), attempt)
            if key in seen:
                continue

            commit_key = (repo, head_sha)
            if commit_key not in commit_cache:
                commit_cache[commit_key] = dependency_files_changed(client, repo, head_sha)
            dependency_files = commit_cache[commit_key]
            conclusion = run.get("conclusion")
            if dependency_files:
                dataset.append(
                    {
                        "repository": repo,
                        "run_id": run_id,
                        "attempt": attempt,
                        "status": conclusion,
                        "commit_sha": head_sha,
                        "created_at": run.get("created_at"),
                        "dependency_files_changed": dependency_files,
                    }
                )
                added += 1
            seen.add(key)

    if added:
        data_path.parent.mkdir(parents=True, exist_ok=True)
        with data_path.open("w", encoding="utf-8") as output_file:
            json.dump(dataset, output_file, indent=2)
    candidate_count = write_candidate_datasets(
        dataset,
        data_path,
        data_path.parent / DEFAULT_FEATURE_CSV.name,
    )
    logger.info(
        "dependency candidate datasets refreshed",
        extra={"candidate_records": candidate_count},
    )
    return added


def main() -> None:
    """Load config and collect records with the shared rate-limited GitHub client."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--group", help=f"repository candidate group from {DEFAULT_GROUPS}")
    selection.add_argument(
        "--repo",
        action="append",
        help="repository to collect as owner/repo; repeat the option to add more than one",
    )
    parser.add_argument("--groups-file", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--output", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()
    if args.group:
        repositories = load_repository_group(args.groups_file, args.group)
    elif args.repo:
        repositories = validate_repository_names(args.repo, "--repo")
    else:
        repositories = load_repositories(args.config)
    if not repositories:
        raise ValueError(f"{args.config} has no enabled repositories")

    with GitHubClient() as client:
        added = collect_dataset(client, repositories, args.output, days=args.days)
    candidate_records, seen = read_existing_data(args.output)
    candidate_count = len(candidate_records)
    logger.info(
        "prototype dataset updated",
        extra={"records_added": added, "total_runs": len(seen), "candidates": candidate_count},
    )
    print(
        f"Added {added} raw records; refreshed {candidate_count} dependency-change candidates "
        f"from {len(seen)} total records."
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
