"""`collector report`: a per-repo data-quality report built from the database alone.

Read-only: it issues SELECTs only and makes no GitHub request, so it needs no token.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from collector.policy import FAILED_CONCLUSIONS
from collector.repo_config import RepoEntry
from collector.store import RepoCounts, RepoQuality, Store

# DEFAULT: a repo is flagged when MORE than half of its stored logs are expired (410): the rest
# of its history is then mostly unusable for log-based features.
EXPIRED_SHARE_LIMIT = 0.5
# DEFAULT: job conclusions that get their own column; every other value (startup_failure,
# neutral, action_required, stale or no conclusion yet) is summed into "other".
JOB_COLUMNS = ("success", "failure", "timed_out", "cancelled", "skipped")
TOTAL_LABEL = "TOTAL"
# DEFAULT: "failed jobs" use the collector's own failed set (policy.FAILED_CONCLUSIONS: failure,
# timed_out, startup_failure), so the report agrees with which logs the log policy fetches.
# DEFAULT: "runs with failed job" counts a run when a job of ANY of its attempts failed, the same
# rule as `fill-logs --passing failed-runs` (D40).


@dataclass
class RepoReport:
    """Everything the report shows for one repo."""

    full_name: str
    enabled: bool = True
    known: bool = False  # present in the database
    counts: RepoCounts = field(default_factory=RepoCounts)
    quality: RepoQuality = field(default_factory=RepoQuality)

    @property
    def failed_jobs(self) -> int:
        """Jobs with a failed conclusion (failure, timed_out or startup_failure)."""
        return sum(n for c, n in self.quality.jobs_by_conclusion.items() if c in FAILED_CONCLUSIONS)

    @property
    def logs_stored(self) -> int:
        """Logs with a raw.logs row, whatever their status."""
        return sum(self.counts.logs.values())

    def jobs_with(self, conclusion: str) -> int:
        """Jobs with exactly this conclusion."""
        return self.quality.jobs_by_conclusion.get(conclusion, 0)

    @property
    def jobs_other(self) -> int:
        """Jobs whose conclusion has no column of its own."""
        # Summed from the conclusions read in the same query, not from `counts.jobs`, so a backfill
        # running at the same time can never make it negative.
        return sum(self.quality.jobs_by_conclusion.values()) - sum(
            self.jobs_with(c) for c in JOB_COLUMNS
        )


def collect_report(store: Store, member: str, entries: Sequence[RepoEntry]) -> list[RepoReport]:
    """Read the quality counts of every configured repo (one row per config entry)."""
    out: list[RepoReport] = []
    with store.transaction():
        stored = {r.full_name: r for r in store.list_repos(member)}
        for entry in entries:
            report = RepoReport(entry.full_name, entry.enabled)
            repo = stored.get(entry.full_name)
            if repo is not None:
                report.known = True
                report.counts = store.repo_counts(repo.id)
                report.quality = store.repo_quality(repo.id)
            out.append(report)
    return out


def total_row(repos: Sequence[RepoReport]) -> RepoReport:
    """The sum of every repo (the earliest first run and the latest last run)."""
    logs: dict[str, int] = {}
    conclusions: dict[str | None, int] = {}
    for r in repos:
        for status, n in r.counts.logs.items():
            logs[status] = logs.get(status, 0) + n
        for conclusion, n in r.quality.jobs_by_conclusion.items():
            conclusions[conclusion] = conclusions.get(conclusion, 0) + n
    firsts = [r.quality.first_run_at for r in repos if r.quality.first_run_at]
    lasts = [r.quality.last_run_at for r in repos if r.quality.last_run_at]
    return RepoReport(
        TOTAL_LABEL,
        known=True,
        counts=RepoCounts(
            runs=sum(r.counts.runs for r in repos),
            attempts=sum(r.counts.attempts for r in repos),
            jobs=sum(r.counts.jobs for r in repos),
            logs=logs,
            commits=sum(r.counts.commits for r in repos),
            logs_pending=sum(r.counts.logs_pending for r in repos),
            runs_skipped=sum(r.counts.runs_skipped for r in repos),
        ),
        quality=RepoQuality(
            jobs_by_conclusion=conclusions,
            runs_with_failed_job=sum(r.quality.runs_with_failed_job for r in repos),
            runs_with_reruns=sum(r.quality.runs_with_reruns for r in repos),
            redactions=sum(r.quality.redactions for r in repos),
            first_run_at=min(firsts) if firsts else None,
            last_run_at=max(lasts) if lasts else None,
        ),
    )


def _expired_share(r: RepoReport) -> float | None:
    total = r.logs_stored
    return r.counts.logs.get("expired", 0) / total if total else None


def find_problems(repos: Sequence[RepoReport]) -> list[str]:
    """Things a person should look at, one line per kind. Disabled repos are not checked."""
    # DEFAULT: disabled repos (reserves, not collected on purpose) never count as problems.
    live = [r for r in repos if r.enabled]
    lines: list[str] = []

    def add(title: str, items: list[str], note: str = "") -> None:
        if items:
            lines.append(f"- {title} ({note or len(items)}): {', '.join(items)}")

    add(
        "Repos with zero runs",
        [
            r.full_name + ("" if r.known else " (not collected yet)")
            for r in live
            if not r.counts.runs
        ],
    )
    jobless = [r.full_name for r in live if r.counts.runs and not r.counts.jobs]
    add("Repos with runs but no jobs", jobless, f"{len(jobless)}, jobs were never collected")
    # DEFAULT: "no failed jobs" is judged only for repos that have jobs, so a repo with no jobs
    # appears once, under "no jobs"; a repo with jobs and no failures gives no failure labels.
    add(
        "Repos with jobs but no failed jobs",
        [r.full_name for r in live if r.counts.jobs and r.failed_jobs == 0],
    )
    add(
        f"Repos where more than {EXPIRED_SHARE_LIMIT:.0%} of the logs are expired",
        [
            f"{r.full_name} ({share:.0%} of {r.logs_stored})"
            for r in live
            if (share := _expired_share(r)) is not None and share > EXPIRED_SHARE_LIMIT
        ],
    )
    # DEFAULT: pending logs and skipped runs are problems for every enabled repo, even when left
    # on purpose (`fill-logs --passing failed-runs`, D40): the report cannot tell the two apart.
    pending = [r for r in live if r.counts.logs_pending]
    add(
        "Repos with logs still pending",
        [f"{r.full_name} {r.counts.logs_pending}" for r in pending],
        f"{len(pending)}, {sum(r.counts.logs_pending for r in pending)} logs in all; "
        "run `collector fill-logs`",
    )
    skipped = [r for r in live if r.counts.runs_skipped]
    add(
        "Repos with skipped runs",
        [f"{r.full_name} {r.counts.runs_skipped}" for r in skipped],
        f"{len(skipped)}, {sum(r.counts.runs_skipped for r in skipped)} runs in all, GitHub "
        "server errors; retried by `collector backfill` or `daily`",
    )
    return lines


def _day(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d") if value else "-"


def _md_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """A padded Markdown table: first column left-aligned, the others right-aligned."""
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]

    def fmt(row: Sequence[str]) -> str:
        cells = [
            row[0].ljust(widths[0]),
            *(c.rjust(w) for c, w in zip(row[1:], widths[1:], strict=True)),
        ]
        return "| " + " | ".join(cells) + " |"

    rule = ["-" * widths[0], *("-" * (w - 1) + ":" for w in widths[1:])]
    return [fmt(header), "| " + " | ".join(rule) + " |", *(fmt(r) for r in rows)]


def _label(r: RepoReport) -> str:
    return r.full_name + ("" if r.enabled else " (disabled)")


def render_report(member: str, repos: Sequence[RepoReport], now: datetime) -> str:
    """Format the data-quality report as Markdown text (readable as plain text too)."""
    # DEFAULT: three narrower tables (runs, jobs, logs), each with a TOTAL row, instead of one
    # 20-column table; the problems come first because that is what a reader needs most.
    rows = [*repos, total_row(repos)]
    lines = [
        f"# PipelineIQ data-quality report: member={member}",
        "",
        f"As of {now.astimezone(UTC):%Y-%m-%d %H:%M} UTC. Read from the database only; "
        "nothing was fetched from GitHub.",
        "",
        "## Problems",
        "",
    ]
    problems = find_problems(repos)
    lines += problems or ["No problems found."]
    lines += ["", "Disabled repos are listed in the tables but not checked here.", ""]

    # DEFAULT: extra columns beyond the task text: re-run runs, runs with failed job, runs
    # skipped, jobs by conclusion and the expired share.
    lines += ["## Runs", ""]
    lines += _md_table(
        ["repo", "runs", "attempts", "re-run runs", "runs with failed job", "first run",
         "last run", "runs skipped"],
        [
            [
                _label(r), str(r.counts.runs), str(r.counts.attempts),
                str(r.quality.runs_with_reruns), str(r.quality.runs_with_failed_job),
                _day(r.quality.first_run_at), _day(r.quality.last_run_at),
                str(r.counts.runs_skipped),
            ]
            for r in rows
        ],
    )  # fmt: skip
    lines += [
        "",
        "A run is one workflow run; an attempt is one try of it. `re-run runs` have more than "
        "one attempt. Dates are the UTC day each run was created.",
        "",
        "## Jobs",
        "",
    ]
    lines += _md_table(
        ["repo", "jobs", *JOB_COLUMNS, "other", "failed jobs"],
        [
            [_label(r), str(r.counts.jobs), *(str(r.jobs_with(c)) for c in JOB_COLUMNS),
             str(r.jobs_other), str(r.failed_jobs)]
            for r in rows
        ],
    )  # fmt: skip
    lines += [
        "",
        "`failed jobs` = failure + timed_out + startup_failure (the collector's rule). "
        "`other` = every conclusion without its own column, such as startup_failure, neutral "
        "or no conclusion yet.",
        "",
        "## Logs",
        "",
    ]
    lines += _md_table(
        ["repo", "logs ok", "expired", "missing", "pending", "redactions", "expired share"],
        [
            [
                _label(r), str(r.counts.logs.get("ok", 0)), str(r.counts.logs.get("expired", 0)),
                str(r.counts.logs.get("missing", 0)), str(r.counts.logs_pending),
                str(r.quality.redactions),
                f"{share:.0%}" if (share := _expired_share(r)) is not None else "-",
            ]
            for r in rows
        ],
    )  # fmt: skip
    lines += [
        "",
        "`expired` logs were gone from GitHub (HTTP 410); `missing` ones could not be read. "
        "`pending` jobs should have a log but have no row yet. `redactions` is the number of "
        "secrets replaced with [REDACTED:...] across the stored logs. `expired share` is "
        "expired divided by all stored log rows.",
    ]
    return "\n".join(lines) + "\n"
