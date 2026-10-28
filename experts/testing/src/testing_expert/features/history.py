"""History at job-key granularity. Uses only outcomes completed strictly before `cutoff`.

A "series" is every finished attempt of one job key (see `loader.SERIES_KEY_SQL`) in one repo and
workflow, in time order. The index answers "what did this series look like just before `cutoff`".
DEFAULT: v1 cut, history is per job key, not per test or per file (needs P4.2 parsed results).
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable
from itertools import accumulate

from ._types import FeatureDict
from .settings import FeatureSettings

DAY = 86_400.0


def history_feature_names(settings: FeatureSettings) -> tuple[str, ...]:
    """Names produced by `SeriesIndex.features` for these settings."""
    names: list[str] = []
    for d in settings.windows_days:
        names += [f"h_runs_{d}d", f"h_fail_rate_{d}d"]
    names += [
        "h_n_prior",
        "h_fail_rate_all",
        "h_weighted_flip_rate",
        "h_consecutive_failures",
        f"h_reruns_{settings.rerun_window_days}d",
        "h_days_since_last_failure",
        "h_last_failed",
    ]
    return tuple(names)


class SeriesIndex:
    """Finished attempts of one job key, sorted by completion time, with prefix sums."""

    __slots__ = ("ts", "fail", "_cum_fail", "_cum_rerun")

    def __init__(self, events: Iterable[tuple[float, int, int, int]]) -> None:
        """`events` are (completed_at epoch, failed 0/1, is_rerun 0/1, tie-break id)."""
        ordered = sorted(events, key=lambda e: (e[0], e[3]))
        self.ts = [e[0] for e in ordered]
        self.fail = [e[1] for e in ordered]
        self._cum_fail = [0, *accumulate(self.fail)]
        self._cum_rerun = [0, *accumulate(e[2] for e in ordered)]

    def features(self, cutoff: float, settings: FeatureSettings) -> FeatureDict:
        """Feature values from events with completed_at strictly before `cutoff`."""
        n = bisect_left(self.ts, cutoff)
        out: FeatureDict = {}
        for d in settings.windows_days:
            lo = bisect_left(self.ts, cutoff - d * DAY, 0, n)
            runs = n - lo
            out[f"h_runs_{d}d"] = runs
            out[f"h_fail_rate_{d}d"] = (
                (self._cum_fail[n] - self._cum_fail[lo]) / runs if runs else None
            )
        out["h_n_prior"] = n
        out["h_fail_rate_all"] = self._cum_fail[n] / n if n else None
        out["h_weighted_flip_rate"] = self._flip_rate(n, settings)
        out["h_consecutive_failures"] = self._consecutive(n, settings.max_consecutive_scan)
        lo = bisect_left(self.ts, cutoff - settings.rerun_window_days * DAY, 0, n)
        out[f"h_reruns_{settings.rerun_window_days}d"] = self._cum_rerun[n] - self._cum_rerun[lo]
        last_fail = next((i for i in range(n - 1, -1, -1) if self.fail[i]), None)
        out["h_days_since_last_failure"] = (
            None if last_fail is None else (cutoff - self.ts[last_fail]) / DAY
        )
        out["h_last_failed"] = self.fail[n - 1] if n else None
        return out

    def _consecutive(self, n: int, cap: int) -> int:
        count = 0
        i = n - 1
        while i >= 0 and self.fail[i] and count < cap:
            count += 1
            i -= 1
        return count

    def _flip_rate(self, n: int, settings: FeatureSettings) -> float | None:
        """Gruber-style weighted flip rate: recent pass/fail flips count more than old ones."""
        seq = self.fail[max(0, n - settings.flip_window) : n]
        if len(seq) < 2:
            return None
        num = den = 0.0
        last = len(seq) - 1
        for i in range(1, len(seq)):
            w = settings.flip_decay ** (last - i)
            den += w
            if seq[i] != seq[i - 1]:
                num += w
        return num / den


EMPTY_SERIES = SeriesIndex(())
