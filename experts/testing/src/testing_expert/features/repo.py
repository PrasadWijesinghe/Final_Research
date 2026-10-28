"""Repo features: average test-job duration and the test framework, both as of `cutoff`."""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from collections.abc import Iterable
from itertools import accumulate

from ._types import FeatureDict
from .history import DAY
from .settings import FeatureSettings

FEATURE_NAMES = ("rp_avg_test_job_minutes", "rp_test_jobs_in_window", "rp_framework")


class DurationIndex:
    """Durations of finished test-like jobs in one repo, with prefix sums."""

    __slots__ = ("ts", "_cum")

    def __init__(self, events: Iterable[tuple[float, float]]) -> None:
        """`events` are (completed_at epoch, duration seconds)."""
        ordered = sorted(events)
        self.ts = [e[0] for e in ordered]
        self._cum = [0.0, *accumulate(e[1] for e in ordered)]

    def average_minutes(self, cutoff: float, window_days: int) -> tuple[float | None, int]:
        """(mean minutes, count) over jobs completed in [cutoff - window, cutoff)."""
        n = bisect_left(self.ts, cutoff)
        lo = bisect_left(self.ts, cutoff - window_days * DAY, 0, n)
        count = n - lo
        if not count:
            return None, 0
        return (self._cum[n] - self._cum[lo]) / count / 60.0, count


EMPTY_DURATIONS = DurationIndex(())


class FrameworkTimeline:
    """Framework seen in package.json snapshots of one repo, by the time each became known."""

    __slots__ = ("ts", "_fw")

    def __init__(self, events: Iterable[tuple[float, str | None]]) -> None:
        """`events` are (first run created_at on the snapshot's commit, framework or None)."""
        ordered = sorted(events, key=lambda e: e[0])
        self.ts = [e[0] for e in ordered]
        self._fw = [e[1] for e in ordered]

    def at(self, cutoff: float) -> str | None:
        """Framework from the newest snapshot that was known strictly before `cutoff`."""
        n = bisect_left(self.ts, cutoff)
        return next((fw for fw in reversed(self._fw[:n]) if fw), None)


EMPTY_TIMELINE = FrameworkTimeline(())


def framework_from_package_json(text: str, frameworks: Iterable[str]) -> str | None:
    """First configured framework named in dependencies / devDependencies, else None."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    names: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict):
            names.update(deps)
    for fw in frameworks:
        if fw in names or f"@{fw}/test" in names:
            return fw
    return None


def framework_from_name(series_key: str, frameworks: Iterable[str]) -> str | None:
    """Fallback: a framework word in the job key."""
    return next(
        (fw for fw in frameworks if re.search(rf"\b{re.escape(fw)}\b", series_key, re.IGNORECASE)),
        None,
    )


def repo_features(
    durations: DurationIndex,
    cutoff: float,
    framework: str | None,
    settings: FeatureSettings,
) -> FeatureDict:
    """Average test-job duration over the window, job count behind it, and the framework."""
    avg, count = durations.average_minutes(cutoff, settings.test_job_window_days)
    return {
        "rp_avg_test_job_minutes": avg,
        "rp_test_jobs_in_window": count,
        "rp_framework": framework,
    }
