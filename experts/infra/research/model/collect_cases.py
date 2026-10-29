"""Collect sanitised public run evidence for human review, never automatic training labels."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .features import REPO_ROOT  # noqa: F401 -- make shared and Infra packages importable


def _get(route: str, *, raw: bool = False) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "PipelineIQ-Research"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    if raw:
        headers["Accept"] = "application/vnd.github.raw+json"
    request = Request("https://api.github.com" + route, headers=headers)
    with urlopen(request, timeout=20) as response:
        content = response.read().decode("utf-8")
    return content if raw else json.loads(content)


def collect_run(repository: str, run_id: int, output_dir: Path, *, challenge: bool = False) -> Path:
    """Save one run's source references and sanitised annotations as unverified evidence."""
    import pandas as pd
    from infra_expert.features.context import workflow_signals

    from shared.sanitise import sanitise

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or run_id < 1:
        raise ValueError("provide owner/repository and a positive numeric run ID")
    prefix = f"/repos/{repository}"
    run = _get(f"{prefix}/actions/runs/{run_id}")
    attempt = int(run["run_attempt"])
    jobs = _get(f"{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    evidence = []
    errors = []
    for job in jobs.get("jobs", []):
        if job.get("conclusion") not in {"failure", "timed_out", "startup_failure"}:
            continue
        check_url = job.get("check_run_url", "")
        expected = f"https://api.github.com{prefix}/check-runs/"
        annotations = []
        if check_url.startswith(expected):
            check_id = check_url.removeprefix(expected)
            if check_id.isdigit():
                try:
                    found = _get(f"{prefix}/check-runs/{check_id}/annotations?per_page=100")
                    annotations = [sanitise(item.get("message", "")).text for item in found]
                except (HTTPError, URLError) as exc:
                    errors.append(f"annotations unavailable: {type(exc).__name__}")
        evidence.append(
            {
                "job_id": job["id"],
                "job_name": sanitise(job.get("name", "")).text,
                "conclusion": job.get("conclusion"),
                "failed_steps": [
                    sanitise(step["name"]).text
                    for step in job.get("steps", [])
                    if step.get("conclusion") == "failure"
                ],
                "annotations": annotations,
                "annotations_attempt_scoped": False,
            }
        )
    workflow_path = run.get("path", "").split("@", 1)[0]
    workflow = ""
    if workflow_path.startswith(".github/workflows/"):
        try:
            workflow = _get(
                f"{prefix}/contents/{quote(workflow_path)}?ref={quote(run['head_sha'])}", raw=True
            )
        except (HTTPError, URLError) as exc:
            errors.append(f"workflow unavailable: {type(exc).__name__}")
    signals = workflow_signals(workflow)
    # JSON must not contain NaN. Preserve missing fields as unknown, not zero.
    signals = {key: None if pd.isna(value) else value for key, value in signals.items()}
    candidate = {
        "repository": repository,
        "run_id": run_id,
        "run_attempt": attempt,
        "run_url": run["html_url"],
        "head_sha": run["head_sha"],
        "run_created_at_utc": run["created_at"],
        "event": run["event"],
        "observed_conclusion": run.get("conclusion"),
        "workflow_path": workflow_path,
        "head_workflow_sanitised": sanitise(workflow).text,
        "workflow_signals": signals,
        "evidence": evidence,
        "collection_errors": errors,
        "jobs_truncated": jobs.get("total_count", 0) > 100,
        "human_verified": False,
        "verified_target": None,
        "reviewer_id": "",
        "evidence_url": "",
        "sanitised_evidence": "",
        "reviewed_at_utc": "",
        "use_for_training": False,
        "dataset_role": "challenge" if challenge else "unreviewed",
        "limitations": [
            "A failed run alone does not prove an infrastructure root cause.",
            "Check annotations may aggregate attempts and are capped at 100 per check.",
            "Head workflow is a proxy; confirm the workflow revision actually executed.",
            "No runtime evidence may be included in pre-run model inputs.",
            "Keep deliberately injected demo failures in a separate challenge set.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{repository.replace('/', '--')}-{run_id}-{attempt}.json"
    # Preserve evidence and any human edits if already collected.
    with path.open("x", encoding="utf-8") as destination:
        destination.write(json.dumps(candidate, indent=2, allow_nan=False))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--challenge", action="store_true", help="Mark deliberately injected faults"
    )
    args = parser.parse_args()
    print(collect_run(args.repo, args.run_id, args.output_dir, challenge=args.challenge))


if __name__ == "__main__":
    main()
