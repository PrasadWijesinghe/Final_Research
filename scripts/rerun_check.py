"""Re-run check (P1.3): is each repo still a good data source? Writes a Markdown + CSV report.

Usage:  uv run --env-file .env python scripts/rerun_check.py --member test [--all]

About 5 REST calls per repo (see `check_repo`); workflow files come from raw.githubusercontent.com
and do not use the REST quota.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from collector.repo_config import ConfigError, RepoEntry, load_member_config
from collector.workflow_detection import (
    DetectionRules,
    detect_relevant_workflow,
    load_detection_rules,
)

from shared.github import GitHubClient, GitHubError, GitHubNotFoundError

logger = logging.getLogger("rerun_check")

REPO_ROOT = Path(__file__).resolve().parents[1]
# DEFAULT: reports go to <repo>/data/reports (git-ignored via `data/`); override with --out-dir.
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "reports"
# DEFAULT: look-back is 90 days, GitHub's retention cap (docs/DATA.md section 1).
WINDOW_DAYS = 90
# GitHub's run list never returns more than 1,000 results (docs/DATA.md section 1).
RESULT_CAP = 1000
WORKFLOW_DIR = ".github/workflows/"
# DEFAULT: these SPDX values mean "GitHub could not tell", so the licence check says `unknown`.
UNKNOWN_LICENCES = {"", "NOASSERTION", "NONE", "OTHER"}


@dataclass
class RepoReport:
    """Everything the check learned about one repo. `None` means unknown (see `error`)."""

    full_name: str
    enabled: bool
    licence_config: str
    licence_github: str | None = None
    default_branch: str | None = None
    archived: bool | None = None
    runs_90d: int | None = None
    failed_90d: int | None = None
    sample_size: int | None = None
    sample_failed: int | None = None
    sample_rerun: int | None = None
    oldest_created: datetime | None = None
    oldest_is_capped: bool = False
    workflows_total: int | None = None
    relevant_workflows: list[str] = field(default_factory=list)
    unfetched_workflows: list[str] = field(default_factory=list)
    missing_configured: list[str] = field(default_factory=list)
    detection_reasons: set[str] = field(default_factory=set)
    api_calls: int = 0
    raw_calls: int = 0
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def failure_rate(self) -> float | None:
        """True failure rate over the whole 90-day window (failed runs / all runs)."""
        if not self.runs_90d or self.failed_90d is None:
            return None
        return self.failed_90d / self.runs_90d

    @property
    def sample_failure_rate(self) -> float | None:
        """Failure rate on the newest-100-runs sample."""
        if not self.sample_size or self.sample_failed is None:
            return None
        return self.sample_failed / self.sample_size

    @property
    def sample_rerun_share(self) -> float | None:
        """Share of the sample whose run_attempt is above 1."""
        if not self.sample_size or self.sample_rerun is None:
            return None
        return self.sample_rerun / self.sample_size

    def retention_days(self, today: date) -> int | None:
        """Age in days of the oldest run still available."""
        if self.oldest_created is None:
            return None
        return (today - self.oldest_created.date()).days

    @property
    def licence_status(self) -> str:
        """`match`, `MISMATCH` or `unknown` (GitHub gave no usable SPDX id)."""
        gh = (self.licence_github or "").strip()
        if gh.upper() in UNKNOWN_LICENCES:
            return "unknown"
        return "match" if gh.lower() == self.licence_config.strip().lower() else "MISMATCH"


def _find_oldest(client: GitHubClient, full_name: str, runs_90d: int, report: RepoReport) -> None:
    """Call 4 (sometimes a 4b): the oldest run still listed, via the last page of per_page=1."""
    path = f"/repos/{full_name}/actions/runs"
    # Guess the last page from the 90-day count (GitHub keeps at most 90 days, so it is usually
    # right). The response's total_count tells us if the guess was wrong; then one more call.
    page = max(1, min(runs_90d, RESULT_CAP))
    body = client.get_json(path, {"per_page": 1, "page": page})
    total = int(body.get("total_count", 0))
    runs = body.get("workflow_runs", [])
    if total == 0:
        return
    if min(total, RESULT_CAP) != page:
        page = min(total, RESULT_CAP)
        body = client.get_json(path, {"per_page": 1, "page": page})
        runs = body.get("workflow_runs", [])
    if total > RESULT_CAP:
        report.oldest_is_capped = True
    if runs:
        report.oldest_created = datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))
    else:
        report.notes.append("could not read the oldest run")


def check_repo(
    client: GitHubClient,
    entry: RepoEntry,
    member: str,
    today: date,
    rules: DetectionRules,
) -> RepoReport:
    """Run the ~5-call check for one repo. A GitHubError ends up in `error`, never raised."""
    report = RepoReport(entry.full_name, entry.enabled, entry.licence)
    calls_before, raw_before = client.api_calls, client.raw_calls
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    runs_path = f"/repos/{entry.full_name}/actions/runs"
    try:
        # Call 1: licence, default branch, archived.
        repo = client.get_json(f"/repos/{entry.full_name}")
        report.licence_github = (repo.get("license") or {}).get("spdx_id")
        report.default_branch = repo.get("default_branch")
        report.archived = bool(repo.get("archived"))
        if report.archived:
            report.notes.append("repo is archived: no new runs will appear")
        if repo.get("full_name") and repo["full_name"].lower() != entry.full_name.lower():
            report.notes.append(f"repo was renamed to {repo['full_name']}")

        # Call 2: total runs in the window + a sample of the newest 100.
        created = f">={since}"
        body = client.get_json(runs_path, {"created": created, "per_page": 100})
        report.runs_90d = int(body.get("total_count", 0))
        sample = body.get("workflow_runs", [])
        report.sample_size = len(sample)
        report.sample_failed = sum(1 for r in sample if r.get("conclusion") == "failure")
        report.sample_rerun = sum(1 for r in sample if int(r.get("run_attempt") or 1) > 1)

        # Call 3: how many runs failed in the window (counted exactly by GitHub).
        body = client.get_json(runs_path, {"status": "failure", "created": created, "per_page": 1})
        report.failed_90d = int(body.get("total_count", 0))

        # Call 4: oldest run still available.
        _find_oldest(client, entry.full_name, report.runs_90d, report)

        # Call 5: workflow paths, then the files themselves from raw.githubusercontent.com.
        workflows = list(
            client.paginate(f"/repos/{entry.full_name}/actions/workflows", items_key="workflows")
        )
        paths = sorted(
            {w["path"] for w in workflows if str(w.get("path", "")).startswith(WORKFLOW_DIR)}
        )
        report.workflows_total = len(paths)
        branch = report.default_branch or "HEAD"
        for path in paths:
            text = client.fetch_raw_file(entry.full_name, branch, path)
            if text is None:
                report.unfetched_workflows.append(path)
                continue
            result = detect_relevant_workflow(text.decode("utf-8", errors="replace"), member, rules)
            report.detection_reasons.add(result.reason)
            if result.relevant:
                report.relevant_workflows.append(path)
        report.missing_configured = [w for w in entry.workflows if w not in paths]
    except GitHubNotFoundError:
        report.error = "repo not found (404), or the token cannot see it"
    except GitHubError as exc:
        hint = (
            " (Actions may be disabled or the token lacks access)" if exc.status_code == 403 else ""
        )
        report.error = f"GitHub error: {exc}{hint}"
    report.api_calls = client.api_calls - calls_before
    report.raw_calls = client.raw_calls - raw_before
    if report.error:
        logger.warning("repo check failed", extra={"repo": entry.full_name, "error": report.error})
    return report


# ---- output ---------------------------------------------------------------------------------

COLUMNS: list[tuple[str, str]] = [
    ("Repo", "GitHub `owner/name`."),
    ("Enabled", "`enabled` flag in the config file (disabled repos only appear with `--all`)."),
    (
        "Runs 90d",
        "Workflow runs created in the last 90 days. Exact count from GitHub (`total_count`).",
    ),
    ("Failed 90d", "Runs in that window whose status is `failure` (exact count from GitHub)."),
    ("Failure rate 90d", "Failed 90d / Runs 90d. This is the true rate, not an estimate."),
    (
        "Sample n",
        "How many runs are in the sample used for the next two columns: the newest 100 runs of "
        "the window (fewer if the repo has fewer).",
    ),
    ("Failure rate (sample)", "Failure rate on that sample only. Biased towards recent runs."),
    (
        "Re-run share (sample)",
        "Share of sample runs with `run_attempt` above 1, i.e. runs that were re-run at least "
        "once. A high value is the flaky-test signal. Sample only.",
    ),
    (
        "Oldest run",
        "Creation date of the oldest run GitHub still lists. If the repo has more than 1,000 "
        "runs the list cannot be read that far back, so the cell says `older than <date>`: "
        "that date is only the 1,000th newest run.",
    ),
    (
        "Retention (days)",
        "Today minus Oldest run: how much history can be collected (GitHub keeps at most 90 "
        "days). Shown with `>` when limited by the 1,000-result cap.",
    ),
    ("Workflows", "Workflow files under `.github/workflows/` that GitHub knows for the repo."),
    (
        "Relevant workflows",
        "Files the P1.2 detection rule keeps for this member (for `test`: a job running "
        "`npm test`, `jest`, `mocha`, `vitest`, ...). Members without a rule keep all files.",
    ),
    ("Licence (config)", "`licence` in `config/repos/<member>.yaml`."),
    (
        "Licence (GitHub)",
        "SPDX id GitHub reports for the repo (`NOASSERTION` or `none` if it cannot tell).",
    ),
    (
        "Licence check",
        "`match`, `MISMATCH` (config and GitHub disagree: fix the config) or `unknown` "
        "(GitHub gave no usable id: check by hand).",
    ),
    ("API calls", "REST calls spent on this repo, including retries. Target: about 5."),
    ("Raw calls", "Workflow files fetched from raw.githubusercontent.com (not REST quota)."),
    ("Notes", "Errors and warnings for this repo. An error leaves the other cells empty."),
]
COLUMN_NAMES = [c[0] for c in COLUMNS]


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.1f}%"


def _num(value: int | None) -> str:
    return "" if value is None else str(value)


def _row_notes(r: RepoReport) -> str:
    notes = list(r.notes)
    if r.error:
        notes.insert(0, f"ERROR: {r.error}")
    if r.unfetched_workflows:
        notes.append("workflow file not fetched (404): " + ", ".join(r.unfetched_workflows))
    if r.missing_configured:
        notes.append("configured workflow not found on GitHub: " + ", ".join(r.missing_configured))
    if "unparseable" in r.detection_reasons:
        notes.append("some workflow files could not be parsed")
    if "no rule for member" in r.detection_reasons:
        notes.append("no detection rule for this member: all workflows kept")
    return "; ".join(notes)


def report_cells(r: RepoReport, today: date) -> list[str]:
    """One report row as strings, in COLUMNS order."""
    retention = r.retention_days(today)
    oldest = ""
    ret = _num(retention)
    if r.oldest_created is not None:
        day = r.oldest_created.date().isoformat()
        oldest = f"older than {day}" if r.oldest_is_capped else day
        ret = f">{retention}" if r.oldest_is_capped else ret
    return [
        r.full_name,
        "yes" if r.enabled else "no",
        _num(r.runs_90d),
        _num(r.failed_90d),
        _pct(r.failure_rate),
        _num(r.sample_size),
        _pct(r.sample_failure_rate),
        _pct(r.sample_rerun_share),
        oldest,
        ret,
        _num(r.workflows_total),
        ";".join(r.relevant_workflows),
        r.licence_config,
        r.licence_github or ("" if r.error else "none"),
        "" if r.error else r.licence_status,
        str(r.api_calls),
        str(r.raw_calls),
        _row_notes(r),
    ]


def render_csv(reports: Sequence[RepoReport], today: date) -> str:
    """The CSV report (header + one row per repo)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMN_NAMES)
    for r in reports:
        writer.writerow(report_cells(r, today))
    return buf.getvalue()


def _md(cell: str) -> str:
    return cell.replace("|", "\\|").replace(";", "; ").replace("\n", " ")


def render_markdown(reports: Sequence[RepoReport], member: str, today: date, skipped: int) -> str:
    """The Markdown report: summary, table, legend."""
    errors = [r for r in reports if r.error]
    mismatches = [r for r in reports if not r.error and r.licence_status == "MISMATCH"]
    total_api = sum(r.api_calls for r in reports)
    average = f" (average {total_api / len(reports):.1f} per repo)" if reports else ""
    lines = [
        f"# Re-run check: member `{member}`, {today.isoformat()}",
        "",
        f"- Repos checked: {len(reports)} (skipped as disabled: {skipped})",
        f"- Window: runs created {(today - timedelta(days=WINDOW_DAYS)).isoformat()} or later "
        f"({WINDOW_DAYS} days, GitHub's retention cap)",
        f"- REST calls used: {total_api}{average}",
        f"- Repos with errors: {len(errors)}"
        + (": " + ", ".join(r.full_name for r in errors) if errors else ""),
        f"- Licence mismatches: {len(mismatches)}"
        + (": " + ", ".join(r.full_name for r in mismatches) if mismatches else ""),
        "",
        "| " + " | ".join(COLUMN_NAMES) + " |",
        "|" + "|".join("---" for _ in COLUMN_NAMES) + "|",
    ]
    lines += ["| " + " | ".join(_md(c) for c in report_cells(r, today)) + " |" for r in reports]
    lines += ["", "## Legend", "", "| Column | Meaning |", "|---|---|"]
    lines += [f"| {name} | {_md(text)} |" for name, text in COLUMNS]
    lines += [
        "",
        "## How to read this",
        "",
        "- Cells marked *sample* use only the newest 100 runs, so they are rough. "
        "`Failure rate 90d` uses every run and is exact.",
        "- `Oldest run` comes from the last page of the unfiltered run list, which GitHub cuts "
        "off at 1,000 results.",
        "- Empty cells on a row with an ERROR note mean that call did not happen or failed.",
        "- Generated by `scripts/rerun_check.py`. Reports are not committed (`data/` is "
        "git-ignored).",
        "",
    ]
    return "\n".join(lines)


def write_reports(
    reports: Sequence[RepoReport], member: str, today: date, skipped: int, out_dir: Path
) -> Path:
    """Write the .md and .csv reports; return the Markdown path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"rerun_check-{member}-{today.isoformat()}"
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(render_markdown(reports, member, today, skipped), encoding="utf-8")
    (out_dir / f"{stem}.csv").write_text(render_csv(reports, today), encoding="utf-8", newline="")
    return md_path


def main(
    argv: Sequence[str] | None = None,
    *,
    client: GitHubClient | None = None,
    today: date | None = None,
) -> int:
    """Run the check. `client` and `today` are injectable for tests."""
    parser = argparse.ArgumentParser(
        prog="rerun_check", description="Re-run check: are the configured repos still good sources?"
    )
    parser.add_argument("--member", required=True, help="dependency | build | test | infra")
    parser.add_argument("--all", action="store_true", help="include repos with enabled: false")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--config-dir", type=Path, default=None, help="default: the repo's config/")
    args = parser.parse_args(argv)

    try:
        config = load_member_config(args.member, args.config_dir)
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
    skipped = len(config.repos) - len(entries)
    day = today or datetime.now(UTC).date()
    gh = client or GitHubClient()
    try:
        reports = [check_repo(gh, e, config.member, day, rules) for e in entries]
    finally:
        if client is None:
            gh.close()
    path = write_reports(reports, config.member, day, skipped, args.out_dir)
    print(f"Report written: {path}")
    print(f"CSV written:    {path.with_suffix('.csv')}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
