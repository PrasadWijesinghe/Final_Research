"""Build screen: how many build/lint failures does each Build repo really have, and how long would
its backfill take? Writes a Markdown + CSV report to data/reports/.

Usage:  uv run --env-file .env python scripts/build_screen.py [--all] [--repo owner/name]

About 30 to 45 REST calls per repo. Run and failure counts are summed over 7-day windows with the
hardened client (`count_workflow_runs`), because GitHub answers big filtered queries with a silent
`total_count: 0` and stops counting at 2,500 (see scripts/README.md, re-run check). Workflow files
come from raw.githubusercontent.com and do not use the REST quota. Run it before the first
`collector backfill --member build`, and reserve repos that are too quiet or too expensive
(docs/experts/build.md section 2).
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from build_expert.labels import is_build_command
from collector.backfill import workflow_path_of
from collector.repo_config import ConfigError, RepoEntry, load_member_config
from collector.workflow_detection import (
    DetectionRules,
    detect_relevant_workflow,
    load_detection_rules,
)
from rerun_check import count_windows

from shared.github import GitHubClient, GitHubError, GitHubNotFoundError

logger = logging.getLogger("build_screen")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "reports"
WINDOW_DAYS = 90  # GitHub's retention cap (docs/DATA.md section 1)
# DEFAULT: the newest 3 pages of 100 runs are the sample for "which share of runs are in build
# workflows" and for the failed runs to open (unfiltered lists, which GitHub answers reliably).
SAMPLE_PAGES = 3
# DEFAULT: failed build-workflow runs whose jobs are opened to see which step failed, per repo.
SAMPLE_FAILED_RUNS = 12
# DEFAULT: thresholds for the suggestion column: about 100+ build failures per repo over the
# collection period, and pass 1 of a repo in about 5 hours with one token, so 20 repos finish in
# about 2 days (GitHub deletes one day of history every day).
MIN_BUILD_FAILURES = 50
MAX_PASS1_CALLS = 24_000
# DEFAULT: measured in the Test member's real backfill (handover, 25 Oct 2026): pass 1 costs about
# 1.6 REST calls per run, and one log download takes about 2.3 s in one lane. Log downloads did
# not appear to count against the REST limit.
CALLS_PER_RUN = 1.6
SECONDS_PER_LOG = 2.3
CALLS_PER_HOUR = 4_800  # 5,000 per token per hour, minus the client's safety margin


@dataclass
class ScreenReport:
    """What the screen learned about one repo. None means unknown (see `error`)."""

    full_name: str
    enabled: bool
    licence_config: str
    licence_github: str | None = None
    workflows_total: int = 0
    build_workflows: int = 0
    runs_90d: int | None = None  # all runs of the repo, every workflow
    failed_90d: int | None = None
    counts_capped: bool = False  # a 7-day window hit GitHub's 2,500 count cap: "at least"
    sample_runs: int = 0
    sample_build_runs: int = 0  # sampled runs in build-relevant workflows
    sample_failed: int = 0
    sample_build_failed: int = 0
    opened_failed_runs: int = 0
    opened_build_failures: int = 0  # opened runs whose failed step builds, lints or type-checks
    failed_jobs_per_failed_run: float | None = None
    api_calls: int = 0
    error: str | None = None

    @property
    def build_run_share(self) -> float | None:
        """Share of sampled runs that belong to build-relevant workflows."""
        return self.sample_build_runs / self.sample_runs if self.sample_runs else None

    @property
    def est_build_runs(self) -> int | None:
        """Estimated runs of build-relevant workflows in 90 days (what pass 1 collects)."""
        share = self.build_run_share
        if self.runs_90d is None or share is None:
            return None
        return round(self.runs_90d * share)

    @property
    def est_build_workflow_failures(self) -> int | None:
        """Estimated failed runs of build-relevant workflows in 90 days."""
        if self.failed_90d is None:
            return None
        if self.sample_failed:
            return round(self.failed_90d * self.sample_build_failed / self.sample_failed)
        share = self.build_run_share
        return None if share is None else round(self.failed_90d * share)

    @property
    def build_share(self) -> float | None:
        """Share of opened failed runs whose failed step builds, lints, formats or type-checks."""
        if not self.opened_failed_runs:
            return None
        return self.opened_build_failures / self.opened_failed_runs

    @property
    def est_build_failures(self) -> int | None:
        """Estimated failed runs with a build-type failure in 90 days."""
        failures, share = self.est_build_workflow_failures, self.build_share
        return None if failures is None or share is None else round(failures * share)

    @property
    def est_pass1_calls(self) -> int | None:
        """Estimated REST calls for pass 1 (`backfill --logs failed`) over 90 days."""
        runs = self.est_build_runs
        return None if runs is None else round(runs * CALLS_PER_RUN)

    @property
    def est_failed_log_hours(self) -> float | None:
        """Estimated hours to download the failed-job logs in one lane."""
        failures = self.est_build_workflow_failures
        if failures is None:
            return None
        per_run = self.failed_jobs_per_failed_run or 1.0
        return failures * per_run * SECONDS_PER_LOG / 3600

    @property
    def suggestion(self) -> str:
        """keep / too quiet / expensive / check by hand."""
        if self.error:
            return "check by hand"
        builds, calls = self.est_build_failures, self.est_pass1_calls
        if self.counts_capped or (calls is not None and calls > MAX_PASS1_CALLS):
            return "expensive"
        if builds is None or calls is None:
            return "check by hand"
        if builds < MIN_BUILD_FAILURES:
            return "too quiet"
        return "keep"


def _since(today: date) -> str:
    return (today - timedelta(days=WINDOW_DAYS)).isoformat()


def _failed_step_names(jobs: list[dict[str, Any]]) -> list[str]:
    names = []
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue
        steps = [s for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
        names.append(str(steps[0].get("name") or "") if steps else str(job.get("name") or ""))
    return names


def _relevant_paths(
    gh: GitHubClient, fn: str, branch: str, rules: DetectionRules, r: ScreenReport
) -> set[str]:
    workflows = list(gh.paginate(f"/repos/{fn}/actions/workflows", items_key="workflows"))
    r.workflows_total = len(workflows)
    relevant: set[str] = set()
    for wf in workflows:
        path = str(wf.get("path") or "")
        if not path.startswith(".github/workflows/") or wf.get("state") != "active":
            continue
        text = gh.fetch_raw_file(fn, branch, path)
        if text is None:
            continue
        if detect_relevant_workflow(text.decode("utf-8", "replace"), "build", rules).relevant:
            relevant.add(path)
    r.build_workflows = len(relevant)
    return relevant


def _count_runs(gh: GitHubClient, fn: str, today: date, r: ScreenReport) -> None:
    runs = failed = 0
    for start, end in count_windows(today):
        run_w = gh.count_workflow_runs(fn, start, end)
        fail_w = gh.count_workflow_runs(fn, start, end, {"status": "failure"})
        runs += max(run_w.count, fail_w.count)  # failures are a lower bound for runs
        failed += fail_w.count
        r.counts_capped = r.counts_capped or run_w.capped or fail_w.capped
    r.runs_90d, r.failed_90d = runs, failed


def screen_repo(
    gh: GitHubClient, entry: RepoEntry, today: date, rules: DetectionRules
) -> ScreenReport:
    """Screen one repo; GitHub errors are recorded on the report, not raised."""
    r = ScreenReport(entry.full_name, entry.enabled, entry.licence)
    start_calls = gh.api_calls
    fn = entry.full_name
    try:
        info = gh.get_json(f"/repos/{fn}")
        r.licence_github = ((info.get("license") or {}).get("spdx_id")) or None
        relevant = _relevant_paths(gh, fn, info.get("default_branch") or "main", rules, r)

        sample: list[dict[str, Any]] = []
        for page in range(1, SAMPLE_PAGES + 1):
            body = gh.get_json(f"/repos/{fn}/actions/runs", {"per_page": 100, "page": page})
            runs = body.get("workflow_runs") or []
            sample.extend(runs)
            if len(runs) < 100:
                break
        build_runs = [run for run in sample if workflow_path_of(run) in relevant]
        r.sample_runs, r.sample_build_runs = len(sample), len(build_runs)
        r.sample_failed = sum(1 for run in sample if run.get("conclusion") == "failure")
        failed_build = [run for run in build_runs if run.get("conclusion") == "failure"]
        r.sample_build_failed = len(failed_build)

        _count_runs(gh, fn, today, r)

        failed_jobs = 0
        for run in failed_build[:SAMPLE_FAILED_RUNS]:
            jobs = gh.get_json(f"/repos/{fn}/actions/runs/{run['id']}/jobs", {"per_page": 100})
            names = _failed_step_names(jobs.get("jobs") or [])
            failed_jobs += len(names)
            r.opened_failed_runs += 1
            if any(is_build_command(n) for n in names):
                r.opened_build_failures += 1
        if r.opened_failed_runs:
            r.failed_jobs_per_failed_run = failed_jobs / r.opened_failed_runs
    except GitHubNotFoundError:
        r.error = "not found (renamed, private or deleted?)"
    except GitHubError as exc:
        r.error = f"GitHub error: {exc}"
    r.api_calls = gh.api_calls - start_calls
    return r


COLUMNS = (
    "repo",
    "enabled",
    "licence (config)",
    "licence (GitHub)",
    "build workflows",
    "runs 90d (all)",
    "build-workflow share",
    "est. build-workflow runs",
    "est. build-workflow failures",
    "build share of failures",
    "est. build failures 90d",
    "pass 1 hours",
    "failed-log hours",
    "suggestion",
    "notes",
)


def _num(value: int | None, capped: bool = False) -> str:
    if value is None:
        return "?"
    return f"≥{value}" if capped else str(value)


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0%}"


def _hours(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}"


def report_cells(r: ScreenReport) -> list[str]:
    """One table row."""
    calls = r.est_pass1_calls
    notes = []
    if r.licence_github and r.licence_github != r.licence_config:
        notes.append(f"licence differs: GitHub says {r.licence_github}")
    if r.counts_capped:
        notes.append("GitHub stopped counting at 2,500 in a week: very busy")
    if r.sample_runs and not r.sample_build_runs:
        notes.append("no recent runs in build workflows")
    if r.error:
        notes.append(r.error)
    return [
        r.full_name,
        "yes" if r.enabled else "no",
        r.licence_config,
        r.licence_github or "?",
        str(r.build_workflows),
        _num(r.runs_90d, r.counts_capped),
        _pct(r.build_run_share),
        _num(r.est_build_runs, r.counts_capped),
        _num(r.est_build_workflow_failures, r.counts_capped),
        _pct(r.build_share),
        _num(r.est_build_failures, r.counts_capped),
        _hours(None if calls is None else calls / CALLS_PER_HOUR),
        _hours(r.est_failed_log_hours),
        r.suggestion,
        "; ".join(notes),
    ]


def render_markdown(reports: Sequence[ScreenReport], today: date) -> str:
    """The Markdown report."""
    lines = [
        f"# Build screen, {today.isoformat()}",
        "",
        f"Window: runs created since {_since(today)} (90 days), counted in 7-day windows. "
        f'"Build-workflow share" is the share of the newest {SAMPLE_PAGES * 100} runs that '
        f'belong to workflows the build rule keeps. "Build share of failures" is the share of '
        f"opened failed runs (up to {SAMPLE_FAILED_RUNS}) whose failed step builds, lints, "
        "formats or type-checks. Pass 1 hours = `backfill --logs failed` with one token "
        f"({CALLS_PER_RUN} calls per run); failed-log hours = downloading the failed-job logs in "
        f"one lane ({SECONDS_PER_LOG} s per log).",
        f"Suggestion: keep at {MIN_BUILD_FAILURES}+ estimated build failures and at most "
        f"{MAX_PASS1_CALLS:,} calls ({MAX_PASS1_CALLS / CALLS_PER_HOUR:.1f} hours) for pass 1.",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("---" for _ in COLUMNS) + "|",
    ]
    for r in reports:
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in report_cells(r)) + " |")
    kept = [r for r in reports if r.suggestion == "keep"]
    calls = sum(r.est_pass1_calls or 0 for r in kept)
    logs = sum(r.est_failed_log_hours or 0.0 for r in kept)
    lines += [
        "",
        f"Repos marked keep: {len(kept)}. Pass 1 for them: about {calls:,} calls "
        f"({calls / CALLS_PER_HOUR:.0f} hours with one token), plus about {logs:.0f} hours of "
        "failed-log downloads in one lane (several lanes share this time).",
        "",
    ]
    return "\n".join(lines)


def render_csv(reports: Sequence[ScreenReport]) -> str:
    """The CSV report."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLUMNS)
    for r in reports:
        writer.writerow(report_cells(r))
    return buf.getvalue()


def main(
    argv: Sequence[str] | None = None,
    *,
    client: GitHubClient | None = None,
    today: date | None = None,
) -> int:
    """Run the screen. `client` and `today` are injectable for tests."""
    parser = argparse.ArgumentParser(prog="build_screen", description=__doc__)
    parser.add_argument("--all", action="store_true", help="include repos with enabled: false")
    parser.add_argument("--repo", action="append", help="only this owner/name (repeatable)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--config-dir", type=Path, default=None, help="default: the repo's config/")
    args = parser.parse_args(argv)
    try:
        config = load_member_config("build", args.config_dir)
        rules = load_detection_rules(
            args.config_dir / "workflow_detection.yaml" if args.config_dir else None
        )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if client is None and not os.environ.get("GITHUB_TOKEN"):
        print(
            "GITHUB_TOKEN is not set. Put it in .env and run with `uv run --env-file .env ...`.",
            file=sys.stderr,
        )
        return 2
    entries = config.repos if args.all else config.enabled_repos()
    if args.repo:
        entries = [e for e in config.repos if e.full_name in args.repo]
    day = today or datetime.now(UTC).date()
    gh = client or GitHubClient()
    try:
        reports = []
        for e in entries:
            logger.info("screening %s", e.full_name)
            reports.append(screen_repo(gh, e, day, rules))
    finally:
        if client is None:
            gh.close()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    md = args.out_dir / f"build_screen-{day.isoformat()}.md"
    md.write_text(render_markdown(reports, day), encoding="utf-8")
    md.with_suffix(".csv").write_text(render_csv(reports), encoding="utf-8")
    print(f"Report written: {md}")
    print(f"CSV written:    {md.with_suffix('.csv')}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
