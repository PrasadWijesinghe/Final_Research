"""Fetch and heuristically label a reproducible sample of failed dependency runs."""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shared.github import GitHubClient, GitHubError
from shared.sanitise import SanitiseResult, sanitise

logger = logging.getLogger(__name__)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATASET = (
    REPOSITORY_ROOT / "data/datasets/dependency/prototype/dependency_runs_dataset.json"
)
DEFAULT_OUTPUT = REPOSITORY_ROOT / "data/datasets/dependency/labeled_sample_50.json"
# DEFAULT: a small first review batch; use CLI options to choose another size or seed.
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_RANDOM_SEED = 42

DEPENDENCY_ERROR_PATTERNS = (
    ("npm_eresolve", re.compile(r"npm ERR! code ERESOLVE", re.IGNORECASE)),
    ("npm_e404", re.compile(r"npm ERR! code E404", re.IGNORECASE)),
    ("npm_missing_script", re.compile(r"npm ERR! missing script", re.IGNORECASE)),
    ("module_not_found", re.compile(r"(?:Cannot find module|MODULE_NOT_FOUND)", re.IGNORECASE)),
    ("dependency_resolution", re.compile(r"Could not resolve dependency", re.IGNORECASE)),
    ("yarn_package_missing", re.compile(r"yarn ERR! Couldn't find package", re.IGNORECASE)),
    ("pnpm_error", re.compile(r"pnpm ERR! ERR_PNPM_", re.IGNORECASE)),
    (
        "version_not_found",
        re.compile(r"Error: No matching version found for", re.IGNORECASE),
    ),
    ("peer_dependency_conflict", re.compile(r"Peer dependency conflict", re.IGNORECASE)),
)

_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _load_failed_runs(dataset_path: Path) -> list[dict[str, Any]]:
    """Load valid failed-run rows from the prototype dataset."""
    with dataset_path.open(encoding="utf-8") as dataset_file:
        records = json.load(dataset_file)
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError(f"{dataset_path} must contain a JSON array of objects")

    failed_runs = [row for row in records if row.get("status") == "failure"]
    for row in failed_runs:
        repository = row.get("repository")
        if not isinstance(repository, str) or not _REPOSITORY_NAME.fullmatch(repository):
            raise ValueError("failed-run records must have a valid repository owner/name")
        try:
            int(row["run_id"])
            attempt = int(row.get("attempt", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("failed-run records need a numeric run_id and attempt") from exc
        if attempt < 1:
            raise ValueError("failed-run attempt must be positive")
    return failed_runs


def _matched_pattern_ids(log_text: str) -> list[str]:
    """Return safe identifiers for dependency-error patterns found in a log."""
    return [
        identifier for identifier, pattern in DEPENDENCY_ERROR_PATTERNS if pattern.search(log_text)
    ]


def classify_failed_run(
    client: GitHubClient,
    row: dict[str, Any],
    *,
    sanitise_fn: Callable[[str], SanitiseResult] = sanitise,
) -> dict[str, Any]:
    """Fetch and classify failed-job logs for one run attempt without retaining log text."""
    repository = str(row["repository"])
    run_id = int(row["run_id"])
    attempt = int(row.get("attempt", 1))
    path = f"/repos/{repository}/actions/runs/{run_id}/jobs"
    jobs = client.paginate(path, {"filter": "all"}, items_key="jobs")

    log_results: list[dict[str, Any]] = []
    redaction_count = 0
    matched_patterns: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if int(job.get("run_attempt", 1)) != attempt or job.get("conclusion") != "failure":
            continue
        job_id = job.get("id")
        if job_id is None:
            continue
        try:
            log_bytes = client.get_bytes(f"/repos/{repository}/actions/jobs/{int(job_id)}/logs")
        except GitHubError as exc:
            if exc.status_code not in (403, 410):
                raise
            status = "forbidden" if exc.status_code == 403 else "expired"
            logger.warning(
                "job log unavailable",
                extra={
                    "repo": repository,
                    "run_id": run_id,
                    "job_id": int(job_id),
                    "status": status,
                },
            )
            log_results.append(
                {"job_id": int(job_id), "log_status": status, "matched_patterns": []}
            )
            continue

        if log_bytes is None:
            log_results.append(
                {"job_id": int(job_id), "log_status": "missing", "matched_patterns": []}
            )
            continue

        clean_log = sanitise_fn(log_bytes.decode("utf-8", errors="replace"))
        redaction_count += clean_log.total
        job_matches = _matched_pattern_ids(clean_log.text)
        matched_patterns.update(job_matches)
        log_results.append(
            {"job_id": int(job_id), "log_status": "ok", "matched_patterns": job_matches}
        )

    all_failed_job_logs_fetched = bool(log_results) and all(
        item["log_status"] == "ok" for item in log_results
    )
    if matched_patterns:
        label = "dependency_error_candidate"
    elif all_failed_job_logs_fetched:
        label = "no_dependency_pattern_match"
    else:
        label = "undetermined"

    return {
        "repository": repository,
        "run_id": run_id,
        "attempt": attempt,
        "commit_sha": row.get("commit_sha"),
        "status": row.get("status"),
        "label_category": label,
        "log_fetched": any(item["log_status"] == "ok" for item in log_results),
        "redaction_count": redaction_count,
        "matched_patterns": sorted(matched_patterns),
        "jobs": log_results,
    }


def sample_and_label_dataset(
    client: GitHubClient,
    dataset_path: Path = DEFAULT_DATASET,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_RANDOM_SEED,
    sanitise_fn: Callable[[str], SanitiseResult] = sanitise,
) -> list[dict[str, Any]]:
    """Label a reproducible sample of failed runs and write safe metadata to JSON."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    failed_runs = _load_failed_runs(dataset_path)
    ordered_runs = sorted(
        failed_runs,
        key=lambda row: (
            str(row["repository"]),
            int(row["run_id"]),
            int(row.get("attempt", 1)),
        ),
    )
    selected = random.Random(seed).sample(ordered_runs, min(sample_size, len(ordered_runs)))
    results = [classify_failed_run(client, row, sanitise_fn=sanitise_fn) for row in selected]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(results, output_file, indent=2)
    logger.info(
        "dependency failure sample labelled",
        extra={"selected": len(results), "output": str(output_path)},
    )
    return results


def main() -> None:
    """Run the sample labeler with the shared GitHub client."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    args = parser.parse_args()

    with GitHubClient() as client:
        results = sample_and_label_dataset(
            client,
            args.dataset,
            args.output,
            sample_size=args.sample_size,
            seed=args.seed,
        )
    counts: dict[str, int] = {}
    for result in results:
        category = str(result["label_category"])
        counts[category] = counts.get(category, 0) + 1
    print(f"Saved {len(results)} run labels to {args.output}")
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
