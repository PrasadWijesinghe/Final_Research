"""Inspect sanitized job-log diagnostics for XGBoost false negatives without saving logs."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

from dependency_expert.features.log_features import (
    extract_sanitized_log_features,
)

from experts.dependency.research.prototype.extract_log_features import (
    MAX_JOB_LOG_BYTES,
    _job_ids,
    _source_rows,
)
from shared.github import GitHubClient, GitHubConfig, GitHubError, GitHubResponseTooLargeError
from shared.sanitise import sanitise

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = REPOSITORY_ROOT / "data/datasets/dependency"
MISCLASSIFIED_CSV = DATA_DIR / "misclassified_records_best_model.csv"
MASTER_CSV = DATA_DIR / "master_training_table.csv"
logger = logging.getLogger("dependency_false_negative_review")

SIGNATURES = {
    "peer_dependency_conflict": re.compile(
        r"ERESOLVE|ERR_PNPM_PEER_DEP_ISSUES|YN0002|YN0060|YN0086|"
        r"peer (?:dep(?:endency)?|requirement)|conflicting peer|unable to resolve dependency",
        re.IGNORECASE,
    ),
    "native_binding_or_build": re.compile(
        r"node-gyp|node-pre-gyp|prebuild-install|gyp ERR!|ERR_DLOPEN_FAILED|"
        r"NODE_MODULE_VERSION|compiled against a different Node|wrong ELF|invalid ELF|"
        r"native binding|native module|"
        r"(?:failed|unable|could not) to find package .*?(?:binding|native)|"
        r"could not load .*?(?:binding|binary)",
        re.IGNORECASE,
    ),
    "package_manager_cache": re.compile(
        r"(?:pnpm|npm|yarn).*cache (?:is )?not found|cache (?:directory|store).*not found",
        re.IGNORECASE,
    ),
    "toolchain_or_engine": re.compile(
        r"EBADENGINE|unsupported engine|engine .*?(?:incompatible|unsupported)|"
        r"requires? (?:node(?:\.js)?|npm|python|rust|cargo)\s*(?:version\s*)?[<>=^~0-9]|"
        r"not supported (?:by|with) .*?(?:node|npm|python)|incompatible .*?(?:node|npm|python)|"
        r"unsupported .*?(?:node|npm|python|typescript|compiler)",
        re.IGNORECASE,
    ),
    "package_manager_resolution": re.compile(
        r"ERR_PNPM_[A-Z0-9_]+|YN\d{4}|ETARGET|ENOVERSIONS|ELOCKVERIFY|"
        r"no matching version|package .*? not found|could not resolve (?:package|dependency)|"
        r"lockfile .*?(?:mismatch|incompatible|out of date)|incompatible lockfile",
        re.IGNORECASE,
    ),
    "compiler_or_native_toolchain": re.compile(
        r"fatal error:|undefined reference to|cannot find -l\w+|CMake Error|"
        r"command .*?(?:gcc|g\+\+|clang|rustc|cargo).*?(?:failed|error)|make: \*\*\*|"
        r"failed to compile|compilation failed|error: failed to run custom build command",
        re.IGNORECASE,
    ),
}
ERROR_LINE = re.compile(
    r"error|failed|failure|not found|unsupported|incompatible|conflict|invalid|"
    r"cannot|could not|unable|denied|exception|fatal|exit code",
    re.IGNORECASE,
)


def _fetch_log(client: GitHubClient, repository: str, job_id: int) -> bytes | None:
    """Fetch a bounded job log, retrying transport failures only."""
    for attempt in range(3):
        try:
            return client.get_bytes(
                f"/repos/{repository}/actions/jobs/{job_id}/logs",
                max_bytes=MAX_JOB_LOG_BYTES,
            )
        except GitHubResponseTooLargeError:
            return None
        except GitHubError as error:
            if error.status_code is not None or attempt == 2:
                logger.warning(
                    "Could not fetch false-negative log repository=%s job_id=%s status=%s",
                    repository,
                    job_id,
                    error.status_code or "transport_error",
                )
                return None
            time.sleep(0.5 * (2**attempt))
    return None


def analyze_false_negatives(
    misclassified_csv: Path = MISCLASSIFIED_CSV,
    master_csv: Path = MASTER_CSV,
) -> dict[str, Any]:
    """Fetch false-negative logs and print only sanitized candidate diagnostic lines."""
    with misclassified_csv.open(encoding="utf-8", newline="") as input_file:
        review_rows = list(csv.DictReader(input_file))
    false_negatives = [
        row
        for row in review_rows
        if row.get("verified_label") == "dependency_failure"
        and row.get("predicted_label") == "other_failure"
    ]
    with master_csv.open(encoding="utf-8", newline="") as input_file:
        master_rows = list(csv.DictReader(input_file))
    master_by_key = {
        (row["repository"], row["run_id"], row["attempt"]): row for row in master_rows
    }

    sources: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    failed_ids: set[tuple[str, int]] = set()
    for row in false_negatives:
        master = master_by_key[(row["repository"], row["run_id"], row["attempt"])]
        source_name = master["source_file"]
        if source_name not in sources:
            records = _source_rows(DATA_DIR / source_name)
            sources[source_name] = {
                (
                    str(record.get("repository", "")),
                    str(record.get("run_id", "")),
                    str(int(record.get("attempt", 1))),
                ): record
                for record in records
            }
        source = sources[source_name].get(
            (row["repository"], row["run_id"], str(int(row["attempt"])))
        )
        failed_ids.update((row["repository"], job_id) for job_id in _job_ids(source or {}))

    config = replace(GitHubConfig.from_env(), timeout=15.0, max_retries=0)
    categories: Counter[str] = Counter()
    examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    run_hits: dict[str, set[tuple[str, str]]] = defaultdict(set)
    unmatched_lines: Counter[str] = Counter()
    unmatched_runs: dict[str, set[tuple[str, str]]] = defaultdict(set)
    fetched_logs = 0

    with GitHubClient(config=config) as client, ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_fetch_log, client, repository, job_id): (repository, job_id)
            for repository, job_id in failed_ids
        }
        logs: list[tuple[str, int, bytes | None]] = []
        for future in as_completed(futures):
            repository, job_id = futures[future]
            content = future.result()
            if content is not None:
                fetched_logs += 1
            logs.append((repository, job_id, content))

    job_to_runs: dict[tuple[str, int], set[tuple[str, str]]] = defaultdict(set)
    for row in false_negatives:
        master = master_by_key[(row["repository"], row["run_id"], row["attempt"])]
        source = sources[master["source_file"]].get(
            (row["repository"], row["run_id"], str(int(row["attempt"])))
        )
        for job_id in _job_ids(source or {}):
            job_to_runs[(row["repository"], job_id)].add((row["repository"], row["run_id"]))

    for repository, job_id, content in logs:
        if content is None:
            continue
        cleaned = sanitise(content.decode("utf-8", errors="replace")).text
        for line in cleaned.splitlines():
            line = line.strip()
            if not line or not ERROR_LINE.search(line):
                continue
            if any(extract_sanitized_log_features(line).values()):
                continue
            matched_signature = False
            for category, pattern in SIGNATURES.items():
                if not pattern.search(line):
                    continue
                matched_signature = True
                categories[category] += 1
                for run_key in job_to_runs[(repository, job_id)]:
                    run_hits[category].add(run_key)
                if len(examples[category]) < 4:
                    excerpt = re.sub(r"\s+", " ", line)[:240]
                    example = {"repository": repository, "run_id": "", "excerpt": excerpt}
                    run_keys = sorted(job_to_runs[(repository, job_id)])
                    if run_keys:
                        example["run_id"] = run_keys[0][1]
                    if example not in examples[category]:
                        examples[category].append(example)
                break
            if not matched_signature:
                normalized = re.sub(r"\s+", " ", line)[:240]
                unmatched_lines[normalized] += 1
                unmatched_runs[normalized].update(job_to_runs[(repository, job_id)])

    summary = {
        "false_negative_records": len(false_negatives),
        "unique_failed_jobs": len(failed_ids),
        "logs_fetched": fetched_logs,
        "signature_line_counts": dict(categories),
        "records_with_unmatched_signature": {
            category: len(keys) for category, keys in run_hits.items()
        },
        "examples": dict(examples),
        "other_unmatched_error_lines": [
            {
                "count": count,
                "records": len(unmatched_runs[line]),
                "excerpt": line,
                "run_ids": sorted(f"{repo}#{run_id}" for repo, run_id in unmatched_runs[line])[:12],
            }
            for line, count in unmatched_lines.most_common(30)
        ],
    }
    print(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    """Review only model false negatives; never persist log text or excerpts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--misclassified-csv", type=Path, default=MISCLASSIFIED_CSV)
    parser.add_argument("--master-csv", type=Path, default=MASTER_CSV)
    args = parser.parse_args()
    analyze_false_negatives(args.misclassified_csv, args.master_csv)


if __name__ == "__main__":
    main()
