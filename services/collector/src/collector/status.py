"""`collector status`: what is stored per repo, days without runs, sync state and rate limit."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx

from collector.policy import date_windows
from collector.repo_config import RepoEntry
from collector.store import RepoCounts, Store, SyncStateRecord
from shared.github import GitHubClient, GitHubError

logger = logging.getLogger(__name__)

# DEFAULT: gaps are looked for in the last 90 days, the length of GitHub's run retention.
GAP_DAYS_WINDOW = 90
# DEFAULT: show at most this many gap ranges per repo, then "and N more".
MAX_GAP_RANGES = 8


@dataclass(frozen=True)
class RateLimit:
    """GitHub's core REST quota, or why it is unavailable."""

    limit: int | None = None
    remaining: int | None = None
    reset: datetime | None = None
    unavailable: str | None = None


@dataclass
class RepoStatus:
    """Everything `status` shows for one repo."""

    full_name: str
    enabled: bool
    known: bool = False  # present in the database
    counts: RepoCounts = field(default_factory=RepoCounts)
    state: SyncStateRecord | None = None
    first_day: date | None = None
    last_day: date | None = None
    gap_days: list[date] = field(default_factory=list)
    days_checked: int = 0


def find_gaps(days: Sequence[date], today: date, window: int = GAP_DAYS_WINDOW) -> list[date]:
    """Days with no run between the first and last collected day, within the last `window` days."""
    if not days:
        return []
    have = set(days)
    first = max(min(have), today - timedelta(days=window - 1))
    last = max(have)
    out: list[date] = []
    day = first
    while day <= last:
        if day not in have:
            out.append(day)
        day += timedelta(days=1)
    return out


def fetch_rate_limit(client: GitHubClient | None) -> RateLimit:
    """Read `GET /rate_limit` (free of quota); never raises."""
    if client is None:
        return RateLimit(unavailable="GITHUB_TOKEN is not set")
    try:
        core = client.get_rate_limit()["resources"]["core"]
        return RateLimit(
            limit=int(core["limit"]),
            remaining=int(core["remaining"]),
            reset=datetime.fromtimestamp(int(core["reset"]), UTC),
        )
    except (GitHubError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("rate limit unavailable", extra={"error": type(exc).__name__})
        return RateLimit(unavailable=f"request failed ({type(exc).__name__})")


def collect_status(
    store: Store, member: str, entries: Sequence[RepoEntry], today: date
) -> list[RepoStatus]:
    """Read the stored state of every configured repo."""
    out: list[RepoStatus] = []
    with store.transaction():
        stored = {r.full_name: r for r in store.list_repos(member)}
        for entry in entries:
            status = RepoStatus(entry.full_name, entry.enabled)
            repo = stored.get(entry.full_name)
            if repo is not None:
                status.known = True
                status.counts = store.repo_counts(repo.id)
                status.state = store.get_sync_state(repo.id)
                days = store.run_days(repo.id)
                if days:
                    status.first_day, status.last_day = days[0], days[-1]
                    status.gap_days = find_gaps(days, today)
                    start = max(days[0], today - timedelta(days=GAP_DAYS_WINDOW - 1))
                    status.days_checked = (days[-1] - start).days + 1
            out.append(status)
    return out


def _ts(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "never"


def _backfill_state(state: SyncStateRecord | None) -> str:
    if state is None:
        return "not started"
    if state.backfill_cursor:
        try:
            data = json.loads(state.backfill_cursor)
            total = len(
                date_windows(
                    datetime.fromisoformat(data["started_at"]),
                    int(data["days"]),
                    int(data["window_days"]),
                )
            )
            return f"in progress ({int(data['windows_done'])} of {total} windows done)"
        except (ValueError, KeyError, TypeError):
            return "in progress"
    if state.backfill_done_at:
        return f"done {_ts(state.backfill_done_at)}"
    return "not started"


def _ranges(days: Sequence[date]) -> list[str]:
    spans: list[tuple[date, date]] = []
    for day in days:
        if spans and day - spans[-1][1] == timedelta(days=1):
            spans[-1] = (spans[-1][0], day)
        else:
            spans.append((day, day))
    return [f"{a}" if a == b else f"{a} to {b} ({(b - a).days + 1} days)" for a, b in spans]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]

    def fmt(row: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()

    return [fmt(header), "  ".join("-" * w for w in widths), *(fmt(r) for r in rows)]


def render_status(member: str, repos: Sequence[RepoStatus], rate: RateLimit, now: datetime) -> str:
    """Format the status report as plain text."""
    lines = [f"PipelineIQ collector status: member={member} (as of {_ts(now)})", ""]
    if rate.unavailable or rate.reset is None:
        lines.append(f"GitHub rate limit: unavailable ({rate.unavailable})")
    else:
        mins = max(0, round((rate.reset - now).total_seconds() / 60))
        lines.append(
            f"GitHub rate limit (core): limit {rate.limit}, remaining {rate.remaining}, "
            f"resets {_ts(rate.reset)} (in {mins} min)"
        )
    lines.append("")
    rows = [
        [
            r.full_name + ("" if r.enabled else " (disabled)"),
            str(r.counts.runs),
            str(r.counts.attempts),
            str(r.counts.jobs),
            str(r.counts.logs.get("ok", 0)),
            str(r.counts.logs.get("expired", 0)),
            str(r.counts.logs.get("missing", 0)),
            str(r.counts.commits),
        ]
        for r in repos
    ]
    header = ["repo", "runs", "attempts", "jobs", "logs ok", "expired", "missing", "commits"]
    lines += _table(header, rows)
    for r in repos:
        lines += ["", r.full_name]
        if not r.known:
            lines.append("  not collected yet: run `collector backfill` first")
            continue
        lines.append(f"  backfill: {_backfill_state(r.state)}")
        lines.append(
            f"  logs pending: {r.counts.logs_pending}"
            + (" (run `collector fill-logs` to fetch them)" if r.counts.logs_pending else "")
        )
        lines.append(f"  last daily sync: {_ts(r.state.last_daily_sync_at if r.state else None)}")
        if r.first_day and r.last_day:
            lines.append(f"  runs created from {r.first_day} to {r.last_day}")
        if r.gap_days:
            lines.append(
                f"  days with zero runs between first and last collected: {len(r.gap_days)} of "
                f"{r.days_checked} (may just be quiet days, not missing data):"
            )
            shown = _ranges(r.gap_days)
            lines += [f"    {text}" for text in shown[:MAX_GAP_RANGES]]
            if len(shown) > MAX_GAP_RANGES:
                lines.append(f"    ... and {len(shown) - MAX_GAP_RANGES} more ranges")
        elif r.first_day:
            lines.append("  days with zero runs: none")
    return "\n".join(lines) + "\n"
