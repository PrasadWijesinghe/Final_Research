"""Pure rules for the collector: which logs to fetch, date windows, deterministic gzip."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

# DEFAULT: "failed" means failure, timed_out or startup_failure; cancelled and skipped jobs are
# not failures, and their logs carry no signal worth the API call.
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
PASSED_CONCLUSIONS = frozenset({"success"})

# DEFAULT: gzip level 6 (the zlib default); the level is fixed so the bytes are stable.
GZIP_LEVEL = 6

# Conclusions whose job log the policy asks for (when the job is completed).
LOG_CONCLUSIONS = FAILED_CONCLUSIONS | PASSED_CONCLUSIONS


def should_fetch_log(status: str | None, conclusion: str | None, workflow_relevant: bool) -> bool:
    """Log policy (DATA 3.2): every failed job, plus passing jobs in relevant workflows."""
    if status != "completed":
        return False
    if conclusion in FAILED_CONCLUSIONS:
        return True
    return conclusion in PASSED_CONCLUSIONS and workflow_relevant


# `fill-logs --passing`: which passing-job logs are fetched (D40). Logs of failed jobs always are.
PassingMode = Literal["all", "failed-runs"]
PASSING_ALL: Final = "all"
PASSING_FAILED_RUNS: Final = "failed-runs"  # only passing jobs of a run that has a failed job


def gzip_bytes(data: bytes) -> bytes:
    """Gzip with mtime 0 and no file name, so equal input always gives equal bytes."""
    return gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)


def date_windows(now: datetime, days: int, window_days: int) -> list[tuple[datetime, datetime]]:
    """Split [today-`days`, now] into `window_days` windows aligned to UTC midnight.

    Returned newest first, each as an inclusive (start, end) pair with whole seconds; the next
    window starts one second after the previous one ends, so no run is seen twice.
    """
    if days < 1 or window_days < 1:
        raise ValueError("days and window_days must be at least 1")
    now = now.astimezone(UTC).replace(microsecond=0)
    horizon = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0)
    windows: list[tuple[datetime, datetime]] = []
    start = horizon
    step = timedelta(days=window_days)
    while start <= now:
        end = min(start + step - timedelta(seconds=1), now)
        windows.append((start, end))
        start += step
    windows.reverse()
    return windows
