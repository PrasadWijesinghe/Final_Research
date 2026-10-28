"""Extract safe log-derived feature flags and join them to the verified master table."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

from dependency_expert.features.log_features import (
    LOG_FEATURE_COLUMNS,
    extract_sanitized_log_features,
)

from shared.github import (
    GitHubClient,
    GitHubConfig,
    GitHubError,
    GitHubResponseTooLargeError,
)
from shared.sanitise import sanitise

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_DIR = REPOSITORY_ROOT / "data/datasets/dependency"
DEFAULT_MASTER = DEFAULT_DATA_DIR / "master_training_table.csv"
DEFAULT_FEATURE_OUTPUT = DEFAULT_DATA_DIR / "log_derived_features.csv"
DEFAULT_TRAINING_OUTPUT = DEFAULT_DATA_DIR / "master_training_table_with_log_features.csv"
MAX_JOB_LOG_BYTES = 5 * 1024 * 1024
logger = logging.getLogger("dependency_log_features")


def _source_rows(path: Path) -> list[dict[str, Any]]:
    """Load review records from supported local label JSON shapes."""
    with path.open(encoding="utf-8") as input_file:
        data: Any = json.load(input_file)
    if isinstance(data, dict):
        data = data.get("candidates", data.get("controls"))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError(f"{path} must contain an array of review records")
    return data


def _job_ids(source_record: dict[str, Any]) -> list[int]:
    """Return unique job IDs recorded for this reviewed failed run."""
    jobs = source_record.get("jobs")
    if not isinstance(jobs, list):
        return []
    result: list[int] = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("job_id") is None:
            continue
        try:
            job_id = int(job["job_id"])
        except (TypeError, ValueError):
            continue
        if job_id not in result:
            result.append(job_id)
    return result


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    """Write generated CSV output with a stable schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _load_master(master_csv: Path) -> list[dict[str, str]]:
    """Read and validate the existing, deduplicated master table."""
    with master_csv.open(encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError("master training CSV must contain a header")
        required = {"repository", "run_id", "attempt", "verified_label", "source_file"}
        missing = sorted(required - set(reader.fieldnames))
        if missing:
            raise ValueError("master CSV is missing columns: " + ", ".join(missing))
        rows = list(reader)
    if not rows:
        raise ValueError("master training CSV contains no records")
    return rows


def _fetch_run_features(
    client: GitHubClient,
    row: dict[str, str],
    source_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fetch all recorded failed-job logs, sanitize in memory, and aggregate flags."""
    job_ids = _job_ids(source_record or {})
    flags = {column: False for column in LOG_FEATURE_COLUMNS}
    fetched = 0
    unavailable = 0
    redactions = 0
    transport_retries = 0
    transport_failures = 0
    statuses: set[str] = set()

    def fetch_job(job_id: int) -> tuple[int, bytes | None, str, int]:
        retries = 0
        for attempt in range(3):
            try:
                content = client.get_bytes(
                    f"/repos/{row['repository']}/actions/jobs/{job_id}/logs",
                    max_bytes=MAX_JOB_LOG_BYTES,
                )
                return job_id, content, "ok" if content is not None else "missing", retries
            except GitHubResponseTooLargeError:
                return job_id, None, "too_large", retries
            except GitHubError as error:
                # Retry only transport failures. HTTP errors such as 410 are left incomplete.
                if error.status_code is not None or attempt == 2:
                    status = f"http_{error.status_code}" if error.status_code else "request_error"
                    return job_id, None, status, retries
                retries += 1
                time.sleep(0.5 * (2**attempt))
        raise AssertionError("unreachable retry loop")

    # Large CI matrices can have hundreds of failed jobs. Bounded concurrency keeps the
    # scan moving while retaining per-response byte limits and the client's request timeout.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_job, job_id) for job_id in job_ids]
        for future in as_completed(futures):
            _, log_bytes, status, retries = future.result()
            transport_retries += retries
            statuses.add(status)
            if log_bytes is None:
                unavailable += 1
                transport_failures += status == "request_error"
                continue

            cleaned = sanitise(log_bytes.decode("utf-8", errors="replace"))
            redactions += cleaned.total
            job_flags = extract_sanitized_log_features(cleaned.text)
            for column, detected in job_flags.items():
                flags[column] = flags[column] or detected
            fetched += 1

    complete = bool(job_ids) and fetched == len(job_ids)
    if not job_ids:
        status = "source_jobs_missing" if source_record is not None else "source_record_missing"
    elif complete:
        status = "complete"
    else:
        status = "incomplete"

    return {
        "repository": row["repository"],
        "run_id": row["run_id"],
        "attempt": row["attempt"],
        "verified_label": row["verified_label"],
        **{name: int(value) for name, value in flags.items()},
        "log_evidence_complete": int(complete),
        "log_scan_status": status,
        "failed_jobs_expected": len(job_ids),
        "failed_jobs_fetched": fetched,
        "failed_jobs_unavailable": unavailable,
        "transport_retry_count": transport_retries,
        "transport_failure_count": transport_failures,
        "redaction_count": redactions,
        "request_statuses": ",".join(sorted(statuses)),
    }


def extract_training_log_features(
    data_dir: Path,
    master_csv: Path,
    feature_output: Path,
    training_output: Path,
    *,
    client: GitHubClient | None = None,
    refresh: bool = False,
) -> dict[str, int]:
    """Scan reviewed source jobs and write per-run features plus the joined training CSV."""
    master_rows = _load_master(master_csv)
    source_cache: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    features: list[dict[str, Any]] = []
    owns_client = client is None
    if client is None:
        config = replace(GitHubConfig.from_env(), timeout=15.0, max_retries=0)
        client = GitHubClient(config=config)

    try:
        feature_fields = [
            "repository",
            "run_id",
            "attempt",
            "verified_label",
            *LOG_FEATURE_COLUMNS,
            "log_evidence_complete",
            "log_scan_status",
            "failed_jobs_expected",
            "failed_jobs_fetched",
            "failed_jobs_unavailable",
            "transport_retry_count",
            "transport_failure_count",
            "redaction_count",
            "request_statuses",
        ]
        # Reuse completed checkpoint rows, but retry incomplete rows on a resumed run.
        prior_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        if feature_output.exists():
            with feature_output.open(encoding="utf-8", newline="") as checkpoint_file:
                for checkpoint in csv.DictReader(checkpoint_file):
                    key = (
                        checkpoint.get("repository", ""),
                        checkpoint.get("run_id", ""),
                        str(int(checkpoint.get("attempt", "1"))),
                    )
                    prior_by_key[key] = checkpoint

        for index, row in enumerate(master_rows, start=1):
            key = (row["repository"], row["run_id"], str(int(row["attempt"])))
            prior = prior_by_key.get(key)
            prior_statuses = set((prior or {}).get("request_statuses", "").split(","))
            has_all_current_features = prior is not None and all(
                column in prior and prior[column] != "" for column in LOG_FEATURE_COLUMNS
            )
            can_reuse = not refresh and prior is not None and has_all_current_features and (
                prior.get("log_evidence_complete") == "1" or "request_error" not in prior_statuses
            )
            if (
                can_reuse
                and prior is not None
                and prior.get("verified_label") == row["verified_label"]
            ):
                prior.setdefault("transport_retry_count", "0")
                prior.setdefault("transport_failure_count", "0")
                features.append(prior)
                logger.info(
                    "Reusing non-transport checkpoint repository=%s run_id=%s row=%s/%s",
                    row["repository"],
                    row["run_id"],
                    index,
                    len(master_rows),
                )
                continue

            source_name = row.get("source_file", "")
            source_record: dict[str, Any] | None = None
            if source_name:
                if source_name not in source_cache:
                    source_path = data_dir / source_name
                    records = _source_rows(source_path) if source_path.exists() else []
                    source_cache[source_name] = {
                        (
                            str(record.get("repository", "")),
                            str(record.get("run_id", "")),
                            str(int(record.get("attempt", 1))),
                        ): record
                        for record in records
                    }
                key = (row["repository"], row["run_id"], str(int(row["attempt"])))
                source_record = source_cache[source_name].get(key)

            logger.info(
                "Scanning sanitized logs repository=%s run_id=%s attempt=%s row=%s/%s",
                row["repository"],
                row["run_id"],
                row["attempt"],
                index,
                len(master_rows),
            )
            current = _fetch_run_features(client, row, source_record)
            if (
                not refresh
                and prior is not None
                and prior.get("verified_label") == row["verified_label"]
            ):
                for column in LOG_FEATURE_COLUMNS:
                    current[column] = int(
                        bool(int(prior.get(column, 0))) or bool(current[column])
                    )
            features.append(current)

            # Keep a valid checkpoint after every run so an interrupted scan can be resumed
            # manually without losing already extracted safe features.
            _write_csv(feature_output, feature_fields, features)

        # Ensure rows reused from a checkpoint are persisted too, including when the final
        # portion of the master table consists only of reused complete records.
        _write_csv(feature_output, feature_fields, features)
    finally:
        if owns_client:
            client.close()

    features_by_key = {
        (row["repository"], row["run_id"], str(int(row["attempt"]))): row for row in features
    }
    joined: list[dict[str, Any]] = []
    for row in master_rows:
        key = (row["repository"], row["run_id"], str(int(row["attempt"])))
        joined.append(row | features_by_key[key])
    joined_fields = list(master_rows[0]) + [
        name for name in feature_fields if name not in master_rows[0]
    ]
    _write_csv(training_output, joined_fields, joined)

    complete_count = sum(int(feature["log_evidence_complete"]) == 1 for feature in features)
    summary = {
        "records": len(master_rows),
        "complete_records": complete_count,
        "incomplete_records": len(master_rows) - complete_count,
        "transport_retries": sum(
            int(feature.get("transport_retry_count", 0)) for feature in features
        ),
        "transport_failures": sum(
            int(feature.get("transport_failure_count", 0)) for feature in features
        ),
        "flags_detected_records": sum(
            any(feature[name] for name in LOG_FEATURE_COLUMNS) for feature in features
        ),
    }
    print(json.dumps(summary, indent=2))
    print(f"Per-run feature status: {feature_output}")
    print(f"Joined training table: {training_output}")
    return summary


def main() -> None:
    """Scan verified record logs and save only sanitized feature metadata."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--features-output", type=Path, default=DEFAULT_FEATURE_OUTPUT)
    parser.add_argument("--training-output", type=Path, default=DEFAULT_TRAINING_OUTPUT)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rescan all logs and recompute every flag instead of reusing completed checkpoints",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    extract_training_log_features(
        args.data_dir,
        args.master,
        args.features_output,
        args.training_output,
        refresh=args.refresh,
    )


if __name__ == "__main__":
    main()
