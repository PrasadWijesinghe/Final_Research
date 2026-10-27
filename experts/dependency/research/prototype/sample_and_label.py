"""Fetch and heuristically label a reproducible sample of failed dependency runs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from shared.github import (
    GitHubClient,
    GitHubConfig,
    GitHubError,
    GitHubNotFoundError,
    GitHubResponseTooLargeError,
)
from shared.sanitise import SanitiseResult, sanitise

logger = logging.getLogger(__name__)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATASET = (
    REPOSITORY_ROOT / "data/datasets/dependency/prototype/dependency_runs_dataset.json"
)
DEFAULT_OUTPUT = REPOSITORY_ROOT / "data/datasets/dependency/labeled_sample_50.json"
DEFAULT_CANDIDATE_OUTPUT = (
    REPOSITORY_ROOT / "data/datasets/dependency/labeled_candidate_batch_100.json"
)
DEFAULT_CONTROL_OUTPUT = REPOSITORY_ROOT / "data/datasets/dependency/random_control_batch_50.json"
# DEFAULT: a small first review batch; use CLI options to choose another size or seed.
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_RANDOM_SEED = 42
# DEFAULT: cap a live log scan at 100 distinct repository/commit pairs to limit API use.
DEFAULT_CANDIDATE_SCAN_SIZE = 100
MAX_CANDIDATE_SCAN_SIZE = 100
# DEFAULT: inspect at most 100 commits to collect up to 50 no-match controls.
DEFAULT_CONTROL_SAMPLE_SIZE = 50
DEFAULT_CONTROL_SCAN_SIZE = 100
MAX_CONTROL_SCAN_SIZE = 100
# DEFAULT: bound control requests and skip a run immediately after its first transport failure.
CONTROL_REQUEST_TIMEOUT_SECONDS = 15.0
CONTROL_REQUEST_MAX_RETRIES = 0
# DEFAULT: skip logs larger than 5 MiB before sanitisation and pattern matching.
MAX_JOB_LOG_BYTES = 5 * 1024 * 1024

DEPENDENCY_ERROR_PATTERNS = (
    ("npm_eresolve", re.compile(r"npm (?:ERR!|error) code ERESOLVE", re.IGNORECASE)),
    (
        "npm_e404",
        re.compile(r"npm (?:ERR!|error) (?:code E404|404)", re.IGNORECASE),
    ),
    ("module_not_found", re.compile(r"(?:Cannot find module|MODULE_NOT_FOUND)", re.IGNORECASE)),
    (
        "dependency_resolution",
        re.compile(
            r"(?:Could not resolve dependency|unable to resolve dependency tree)", re.IGNORECASE
        ),
    ),
    (
        "yarn_package_missing",
        re.compile(
            r"(?:yarn ERR! Couldn't find package|Couldn't find any versions for|YN0035|YN0082)",
            re.IGNORECASE,
        ),
    ),
    ("pnpm_error", re.compile(r"(?:pnpm ERR! )?ERR_PNPM_[A-Z0-9_]+", re.IGNORECASE)),
    (
        "version_not_found",
        re.compile(r"(?:Error: No matching version found for|ETARGET)", re.IGNORECASE),
    ),
    (
        "peer_dependency_conflict",
        re.compile(r"(?:Peer dependency conflict|peer dep missing)", re.IGNORECASE),
    ),
    ("registry_timeout", re.compile(r"ERR_SOCKET_TIMEOUT", re.IGNORECASE)),
)

_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _configure_cli_logging() -> None:
    """Show this scanner's safe progress messages without logging HTTP request URLs."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _control_client_config() -> GitHubConfig:
    """Return bounded request settings for control scans."""
    return replace(
        GitHubConfig.from_env(),
        timeout=CONTROL_REQUEST_TIMEOUT_SECONDS,
        max_retries=CONTROL_REQUEST_MAX_RETRIES,
    )


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
    logger.info(
        "Fetching failed-job list repository=%s run_id=%s",
        repository,
        run_id,
        extra={"repo": repository, "run_id": run_id, "attempt": attempt},
    )
    try:
        jobs = list(client.paginate(path, {"filter": "all"}, items_key="jobs"))
    except GitHubNotFoundError:
        logger.warning(
            "failed-run jobs unavailable",
            extra={"repo": repository, "run_id": run_id, "status": "not_found"},
        )
        return {
            "repository": repository,
            "run_id": run_id,
            "attempt": attempt,
            "commit_sha": row.get("commit_sha"),
            "status": row.get("status"),
            "label_category": "undetermined",
            "log_fetched": False,
            "redaction_count": 0,
            "matched_patterns": [],
            "jobs": [],
            "log_status": "not_found",
        }

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
            logger.info(
                "Fetching failed-job log repository=%s run_id=%s job_id=%s",
                repository,
                run_id,
                int(job_id),
                extra={
                    "repo": repository,
                    "run_id": run_id,
                    "attempt": attempt,
                    "job_id": int(job_id),
                },
            )
            log_bytes = client.get_bytes(
                f"/repos/{repository}/actions/jobs/{int(job_id)}/logs",
                max_bytes=MAX_JOB_LOG_BYTES,
            )
        except GitHubResponseTooLargeError as exc:
            log_size_bytes = exc.content_length
            logger.warning(
                (
                    "Skipping oversized failed-job log repository=%s run_id=%s "
                    "job_id=%s size_bytes=%s limit_bytes=%s"
                ),
                repository,
                run_id,
                int(job_id),
                log_size_bytes if log_size_bytes is not None else "over_limit",
                exc.max_bytes,
                extra={
                    "repo": repository,
                    "run_id": run_id,
                    "job_id": int(job_id),
                    "log_size_bytes": log_size_bytes,
                    "max_log_size_bytes": exc.max_bytes,
                },
            )
            log_results.append(
                {
                    "job_id": int(job_id),
                    "log_status": "too_large",
                    "log_size_bytes": log_size_bytes,
                    "max_log_size_bytes": exc.max_bytes,
                    "matched_patterns": [],
                }
            )
            continue
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

        if len(log_bytes) > MAX_JOB_LOG_BYTES:
            logger.warning(
                (
                    "Skipping oversized failed-job log repository=%s run_id=%s "
                    "job_id=%s size_bytes=%s limit_bytes=%s"
                ),
                repository,
                run_id,
                int(job_id),
                len(log_bytes),
                MAX_JOB_LOG_BYTES,
                extra={
                    "repo": repository,
                    "run_id": run_id,
                    "job_id": int(job_id),
                    "log_size_bytes": len(log_bytes),
                    "max_log_size_bytes": MAX_JOB_LOG_BYTES,
                },
            )
            log_results.append(
                {
                    "job_id": int(job_id),
                    "log_status": "too_large",
                    "log_size_bytes": len(log_bytes),
                    "max_log_size_bytes": MAX_JOB_LOG_BYTES,
                    "matched_patterns": [],
                }
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


def _select_distinct_failed_runs(
    failed_runs: list[dict[str, Any]],
    *,
    scan_limit: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Choose a deterministic, repository-balanced set with one run per commit."""
    if scan_limit < 1:
        raise ValueError("scan_limit must be positive")
    if scan_limit > MAX_CANDIDATE_SCAN_SIZE:
        raise ValueError(f"scan_limit must not exceed {MAX_CANDIDATE_SCAN_SIZE}")

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in failed_runs:
        repository = str(row["repository"])
        commit_sha = row.get("commit_sha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise ValueError("failed-run records need a commit_sha for distinct-commit sampling")
        commits = grouped.setdefault(repository, {})
        existing = commits.get(commit_sha)
        row_key = (
            str(row.get("created_at", "")),
            int(row["run_id"]),
            int(row.get("attempt", 1)),
        )
        if existing is None or row_key > (
            str(existing.get("created_at", "")),
            int(existing["run_id"]),
            int(existing.get("attempt", 1)),
        ):
            commits[commit_sha] = row

    rng = random.Random(seed)
    repositories = sorted(grouped)
    candidates = {
        repository: [grouped[repository][commit] for commit in sorted(grouped[repository])]
        for repository in repositories
    }
    for repository in repositories:
        rng.shuffle(candidates[repository])

    selected: list[dict[str, Any]] = []
    while len(selected) < scan_limit:
        added_in_round = False
        for repository in repositories:
            if candidates[repository]:
                selected.append(candidates[repository].pop())
                added_in_round = True
                if len(selected) == scan_limit:
                    break
        if not added_in_round:
            break
    return selected


def _load_reviewed_commit_keys(directory: Path) -> set[tuple[str, str]]:
    """Find repository/commit pairs with final labels in local review sample files."""
    reviewed: set[tuple[str, str]] = set()
    for sample_path in sorted(directory.glob("labeled_sample*.json")):
        with sample_path.open(encoding="utf-8") as sample_file:
            records = json.load(sample_file)
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError(f"{sample_path} must contain a JSON array of review records")
        for row in records:
            repository = row.get("repository")
            commit_sha = row.get("commit_sha")
            has_final_label = row.get("is_final") is True or row.get("verified_label") is not None
            jobs = row.get("jobs", [])
            if isinstance(jobs, list):
                has_final_label = has_final_label or any(
                    isinstance(job, dict)
                    and (job.get("is_final") is True or job.get("verified_label") is not None)
                    for job in jobs
                )
            if (
                has_final_label
                and isinstance(repository, str)
                and isinstance(commit_sha, str)
                and commit_sha
            ):
                reviewed.add((repository, commit_sha))
    return reviewed


def _load_candidate_batch_commit_keys(
    directory: Path,
    current_output_path: Path,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Load scanned and final-reviewed commit keys from other candidate batch files."""
    scanned: set[tuple[str, str]] = set()
    reviewed: set[tuple[str, str]] = set()
    current_output = current_output_path.resolve()
    for batch_path in sorted(directory.glob("*candidate*.json")):
        if batch_path.resolve() == current_output:
            continue
        with batch_path.open(encoding="utf-8") as batch_file:
            batch = json.load(batch_file)
        if not isinstance(batch, dict):
            raise ValueError(f"{batch_path} must contain a candidate batch object")

        scanned_keys = batch.get("scanned_commits", [])
        if not isinstance(scanned_keys, list):
            raise ValueError(f"{batch_path} scanned_commits must be a JSON array")
        for key in scanned_keys:
            if not isinstance(key, dict):
                raise ValueError(f"{batch_path} scanned_commits entries must be objects")
            repository = key.get("repository")
            commit_sha = key.get("commit_sha")
            if isinstance(repository, str) and isinstance(commit_sha, str) and commit_sha:
                scanned.add((repository, commit_sha))

        candidates = batch.get("candidates", [])
        if not isinstance(candidates, list) or any(not isinstance(row, dict) for row in candidates):
            raise ValueError(f"{batch_path} candidates must be an array of objects")
        for row in candidates:
            repository = row.get("repository")
            commit_sha = row.get("commit_sha")
            is_final = row.get("is_final") is True or row.get("verified_label") is not None
            if (
                is_final
                and isinstance(repository, str)
                and isinstance(commit_sha, str)
                and commit_sha
            ):
                reviewed.add((repository, commit_sha))

    return scanned, reviewed


def _load_control_batch_commit_keys(
    directory: Path,
    current_output_path: Path,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Load scanned and final-reviewed commit keys from earlier control batches."""
    scanned: set[tuple[str, str]] = set()
    reviewed: set[tuple[str, str]] = set()
    current_output = current_output_path.resolve()
    for batch_path in sorted(directory.glob("random_control*.json")):
        if batch_path.resolve() == current_output:
            continue
        with batch_path.open(encoding="utf-8") as batch_file:
            batch = json.load(batch_file)
        if not isinstance(batch, dict):
            raise ValueError(f"{batch_path} must contain a random control batch object")
        scanned_keys = batch.get("scanned_commits", [])
        if not isinstance(scanned_keys, list):
            raise ValueError(f"{batch_path} scanned_commits must be a JSON array")
        for key in scanned_keys:
            if not isinstance(key, dict):
                raise ValueError(f"{batch_path} scanned_commits entries must be objects")
            repository = key.get("repository")
            commit_sha = key.get("commit_sha")
            if isinstance(repository, str) and isinstance(commit_sha, str) and commit_sha:
                scanned.add((repository, commit_sha))
        controls = batch.get("controls", [])
        if not isinstance(controls, list) or any(not isinstance(row, dict) for row in controls):
            raise ValueError(f"{batch_path} controls must be an array of objects")
        for row in controls:
            repository = row.get("repository")
            commit_sha = row.get("commit_sha")
            is_final = row.get("is_final") is True or row.get("verified_label") is not None
            if (
                is_final
                and isinstance(repository, str)
                and isinstance(commit_sha, str)
                and commit_sha
            ):
                reviewed.add((repository, commit_sha))
    return scanned, reviewed


def scan_dependency_candidates(
    client: GitHubClient,
    dataset_path: Path = DEFAULT_DATASET,
    output_path: Path = DEFAULT_CANDIDATE_OUTPUT,
    *,
    scan_limit: int = DEFAULT_CANDIDATE_SCAN_SIZE,
    seed: int = DEFAULT_RANDOM_SEED,
    sanitise_fn: Callable[[str], SanitiseResult] = sanitise,
) -> list[dict[str, Any]]:
    """Scan a bounded set of distinct failed commits and save only heuristic candidates."""
    reviewed_commits = _load_reviewed_commit_keys(output_path.parent)
    prior_scanned_commits, reviewed_candidate_commits = _load_candidate_batch_commit_keys(
        output_path.parent,
        output_path,
    )
    excluded_commits = reviewed_commits | prior_scanned_commits | reviewed_candidate_commits
    failed_runs = _load_failed_runs(dataset_path)
    selected = _select_distinct_failed_runs(
        [
            row
            for row in failed_runs
            if (str(row["repository"]), str(row["commit_sha"])) not in excluded_commits
        ],
        scan_limit=scan_limit,
        seed=seed,
    )
    candidates: list[dict[str, Any]] = []
    for row in selected:
        result = classify_failed_run(client, row, sanitise_fn=sanitise_fn)
        if not result["matched_patterns"]:
            continue
        for job in result["jobs"]:
            job["job_url"] = (
                f"https://github.com/{result['repository']}/actions/runs/"
                f"{result['run_id']}/job/{job['job_id']}"
            )
            job.update(
                {
                    "verified_label": None,
                    "verified_reason": None,
                    "labelled_by": None,
                    "is_final": False,
                }
            )
        result.update(
            {
                "created_at": row.get("created_at"),
                "label_category": "dependency_error_candidate",
                "verified_label": None,
                "verified_reason": None,
                "labelled_by": None,
                "is_final": False,
            }
        )
        candidates.append(result)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(
            {
                "scan_limit": scan_limit,
                "seed": seed,
                "excluded_reviewed_commits": len(reviewed_commits),
                "excluded_prior_scanned_commits": len(prior_scanned_commits),
                "excluded_reviewed_candidate_commits": len(reviewed_candidate_commits),
                "scanned_runs": len(selected),
                "scanned_commits": [
                    {
                        "repository": str(row["repository"]),
                        "commit_sha": str(row["commit_sha"]),
                    }
                    for row in selected
                ],
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
            output_file,
            indent=2,
        )
    logger.info(
        "dependency log candidate scan complete",
        extra={
            "scanned_runs": len(selected),
            "candidates": len(candidates),
            "output": str(output_path),
        },
    )
    return candidates


def scan_random_failure_controls(
    client: GitHubClient,
    dataset_path: Path = DEFAULT_DATASET,
    output_path: Path = DEFAULT_CONTROL_OUTPUT,
    *,
    sample_size: int = DEFAULT_CONTROL_SAMPLE_SIZE,
    scan_limit: int = DEFAULT_CONTROL_SCAN_SIZE,
    seed: int = DEFAULT_RANDOM_SEED,
    sanitise_fn: Callable[[str], SanitiseResult] = sanitise,
) -> list[dict[str, Any]]:
    """Sample failed runs with fetched logs and no dependency-pattern matches."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    if scan_limit < 1 or scan_limit > MAX_CONTROL_SCAN_SIZE:
        raise ValueError(f"scan_limit must be between 1 and {MAX_CONTROL_SCAN_SIZE}")

    reviewed_sample_commits = _load_reviewed_commit_keys(output_path.parent)
    prior_candidate_scans, reviewed_candidate_commits = _load_candidate_batch_commit_keys(
        output_path.parent,
        output_path,
    )
    prior_control_scans, reviewed_control_commits = _load_control_batch_commit_keys(
        output_path.parent,
        output_path,
    )
    excluded_commits = (
        reviewed_sample_commits
        | prior_candidate_scans
        | reviewed_candidate_commits
        | prior_control_scans
        | reviewed_control_commits
    )
    failed_runs = _load_failed_runs(dataset_path)
    selected = _select_distinct_failed_runs(
        [
            row
            for row in failed_runs
            if (str(row["repository"]), str(row["commit_sha"])) not in excluded_commits
        ],
        scan_limit=scan_limit,
        seed=seed,
    )
    controls: list[dict[str, Any]] = []
    scanned: list[dict[str, Any]] = []
    scan_errors: list[dict[str, Any]] = []
    scan_status = "incomplete"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        for row in selected:
            scanned.append(row)
            repository = str(row["repository"])
            run_id = int(row["run_id"])
            attempt = int(row.get("attempt", 1))
            logger.info(
                "Processing control run repository=%s run_id=%s",
                repository,
                run_id,
                extra={"repo": repository, "run_id": run_id, "attempt": attempt},
            )
            try:
                result = classify_failed_run(client, row, sanitise_fn=sanitise_fn)
            except GitHubError as exc:
                scan_errors.append(
                    {
                        "repository": repository,
                        "run_id": run_id,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "status_code": exc.status_code,
                    }
                )
                logger.warning(
                    "Skipping control run after GitHub request failure",
                    extra={
                        "repo": repository,
                        "run_id": run_id,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "status_code": exc.status_code,
                    },
                )
                continue
            if result["label_category"] != "no_dependency_pattern_match":
                continue
            for job in result["jobs"]:
                job["job_url"] = (
                    f"https://github.com/{result['repository']}/actions/runs/"
                    f"{result['run_id']}/job/{job['job_id']}"
                )
                job.update(
                    {
                        "verified_label": None,
                        "verified_reason": None,
                        "labelled_by": None,
                        "is_final": False,
                    }
                )
            result.update(
                {
                    "created_at": row.get("created_at"),
                    "label_category": "random_control_candidate",
                    "verified_label": None,
                    "verified_reason": None,
                    "labelled_by": None,
                    "is_final": False,
                }
            )
            controls.append(result)
            if len(controls) >= sample_size:
                break
        scan_status = "complete_with_errors" if scan_errors else "complete"
    except KeyboardInterrupt:
        scan_status = "interrupted"
        raise
    finally:
        output_data = {
            "sample_size": sample_size,
            "scan_limit": scan_limit,
            "seed": seed,
            "excluded_commits": len(excluded_commits),
            "scan_status": scan_status,
            "scanned_runs": len(scanned),
            "scanned_commits": [
                {
                    "repository": str(row["repository"]),
                    "commit_sha": str(row["commit_sha"]),
                }
                for row in scanned
            ],
            "scan_errors": scan_errors,
            "control_count": len(controls),
            "controls": controls,
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output_file:
                temporary_path = Path(output_file.name)
                json.dump(output_data, output_file, indent=2)
                output_file.write("\n")
            os.replace(temporary_path, output_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    logger.info(
        "random failure control scan complete",
        extra={
            "scanned_runs": len(scanned),
            "controls": len(controls),
            "scan_errors": len(scan_errors),
            "output": str(output_path),
        },
    )
    return controls


def main() -> None:
    """Run the sample labeler with the shared GitHub client."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--mode",
        choices=("sample", "candidates", "controls"),
        default="sample",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=DEFAULT_CANDIDATE_SCAN_SIZE,
        help=f"maximum distinct repository/commit pairs to scan (1-{MAX_CANDIDATE_SCAN_SIZE})",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    args = parser.parse_args()
    _configure_cli_logging()

    output_path = args.output or (
        DEFAULT_CANDIDATE_OUTPUT
        if args.mode == "candidates"
        else DEFAULT_CONTROL_OUTPUT
        if args.mode == "controls"
        else DEFAULT_OUTPUT
    )
    client_config = None
    if args.mode == "controls":
        client_config = _control_client_config()
    with GitHubClient(config=client_config) as client:
        if args.mode == "candidates":
            results = scan_dependency_candidates(
                client,
                args.dataset,
                output_path,
                scan_limit=args.scan_limit,
                seed=args.seed,
            )
            print(
                f"Scanned at most {args.scan_limit} distinct commits; "
                f"saved {len(results)} heuristic candidates to {output_path}"
            )
        elif args.mode == "controls":
            try:
                results = scan_random_failure_controls(
                    client,
                    args.dataset,
                    output_path,
                    sample_size=args.sample_size,
                    scan_limit=args.scan_limit,
                    seed=args.seed,
                )
            except KeyboardInterrupt:
                print(f"Control scan interrupted; partial progress saved to {output_path}")
                raise SystemExit(130) from None
            print(
                f"Inspected at most {args.scan_limit} distinct commits; "
                f"saved {len(results)} unverified random controls to {output_path}"
            )
        else:
            results = sample_and_label_dataset(
                client,
                args.dataset,
                output_path,
                sample_size=args.sample_size,
                seed=args.seed,
            )
            counts: dict[str, int] = {}
            for result in results:
                category = str(result["label_category"])
                counts[category] = counts.get(category, 0) + 1
            print(f"Saved {len(results)} run labels to {output_path}")
            print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
