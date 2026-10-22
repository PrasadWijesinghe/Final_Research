"""Daily sync: top up runs created since the last sync, plus a 14-day re-scan for new attempts.

Re-runs of older runs show up later as a new attempt of an old run (DATA 3.3). The per-run work is
the backfill's own `_process_run`, which is idempotent: a run whose latest attempt is stored and
completed is skipped, a run with a new attempt is fetched again (attempt metadata, jobs, logs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from collector.backfill import Backfiller, RepoSummary
from collector.repo_config import RepoEntry

logger = logging.getLogger(__name__)

# DEFAULT: 14 days, as written in docs/DATA.md 3.3 (re-runs of older runs appear later).
RESCAN_DAYS = 14


class NotBackfilledError(Exception):
    """The repo has no `last_daily_sync_at` yet: the backfill has not finished."""


@dataclass
class DailySummary(RepoSummary):
    """Counters for one daily sync of one repo."""

    window_start: datetime | None = None
    window_end: datetime | None = None
    gap_days: float = 0.0
    data_lost: bool = False


class DailySyncer(Backfiller):
    """Daily sync for the repos of one member; reuses the backfill's per-run processing."""

    def __init__(self, *args: Any, rescan_days: int = RESCAN_DAYS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.rescan_days = rescan_days

    def daily_repo(self, entry: RepoEntry) -> DailySummary:
        """Sync one repo; `last_daily_sync_at` only moves forward when the whole repo succeeded."""
        fn = entry.full_name
        started = self._clock().replace(microsecond=0)
        repo_id, default_branch, state = self._register_repo(entry)
        last = state.last_daily_sync_at if state else None
        if last is None:
            raise NotBackfilledError(f"{fn}: no daily sync point yet; run backfill first")
        summary = DailySummary(window_end=started)
        # Catch-up: from the older of "last sync" and "re-scan horizon", so gaps are covered.
        start = min(last, started - timedelta(days=self.rescan_days))
        summary.gap_days = round((started - last).total_seconds() / 86400, 1)
        if started - last > timedelta(days=self.days):
            # GitHub keeps runs for about `days` days: older runs can no longer be fetched.
            summary.data_lost = True
            start = max(start, started - timedelta(days=self.days))
            logger.warning(
                "gap since the last daily sync exceeds the retention; runs in it are lost",
                extra={"repo": fn, "gap_days": summary.gap_days, "retention_days": self.days},
            )
        summary.window_start = start
        relevant = self._sync_workflows(entry, repo_id, default_branch)
        for run in self.client.iter_workflow_runs(fn, start, started):
            self._process_run(entry, repo_id, run, relevant, summary)
        with self.store.transaction():
            self.store.set_last_daily_sync(repo_id, started)
        return summary
