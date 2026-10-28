"""Per-test and per-file history of one job series (`ht_*`), from parsed test results (P4.4 v2).

A series is every finished attempt of one job key in one repo and workflow (same as `history.py`).
Here each attempt also carries what the log said: which tests (or files) failed, and how each
test or file ended. Only results of attempts completed strictly before `cutoff` are used.

Granularity, recorded in `ht_test_level`: Mocha's reporter lists every test (test level, but with
no file name); Jest and Vitest usually list only files (file level; a failing test is also named in
the failure details). A unit is a test id (`testing.test_ids`), which is either kind.

DEFAULT: results of passing tests are only stored for tests that have failed somewhere (see
`extract.prune_passing`), so counts like "tests run" are not offered; rates are failures divided by
the runs we saw the unit in, which for a never-failing unit is simply 0 or unknown.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ._types import FeatureDict
from .history import DAY
from .settings import FeatureSettings

PREFIX = "ht_"
# DEFAULT: windows for the "distinct flaky tests" counts, from the task brief (30 and 90 days).
WINDOWS_DAYS = (30, 90)
# DEFAULT: flip rate over the last 20 outcomes of a unit, no decay (units are few and sparse).
FLIP_WINDOW = 20

FEATURE_NAMES: tuple[str, ...] = (
    "ht_test_level",
    "ht_parsed_runs_30d",
    "ht_parsed_runs_90d",
    "ht_last_run_failed_tests",
    "ht_persistent_failing_tests",
    "ht_failed_tests_30d",
    "ht_failed_tests_90d",
    "ht_flaky_tests_30d",
    "ht_flaky_tests_90d",
    "ht_touched_test_files",
    "ht_touched_files_with_history",
    "ht_touched_fail_rate",
    "ht_touched_max_flip_rate",
    "ht_touched_last_failed",
    "ht_touched_failures_90d",
)


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One stored result of one attempt (a `testing.test_results` row plus its attempt facts)."""

    job_id: int
    ts: float  # completed_at of the attempt, epoch seconds
    test_id: str
    file: str | None
    failed: bool
    granularity: str  # "test" or "file" (this row)
    test_level: int = (
        0  # 1 when the job's reporter listed individual tests (log_parses.granularity)
    )


class TestHistory:
    """Per-test and per-file outcomes of one series, in time order."""

    __test__ = False  # not a pytest class
    __slots__ = ("runs", "run_ts", "fail_ts", "fail_ids", "per_unit", "per_file")

    def __init__(self, rows: Iterable[ResultRow], run_ids: Iterable[tuple[float, int, int]] = ()):
        """`rows` are stored results; `run_ids` are (ts, job_id, test_level) of attempts with none.

        An attempt that was parsed but kept no rows still counts as a parsed run.
        """
        by_job: dict[int, list[ResultRow]] = defaultdict(list)
        for r in rows:
            by_job[r.job_id].append(r)
        runs: dict[int, tuple[float, frozenset[str], int]] = {}
        for ts, job_id, level in run_ids:
            runs[job_id] = (ts, frozenset(), level)
        per_unit: dict[str, list[tuple[float, int]]] = defaultdict(list)
        per_file: dict[str, list[tuple[float, int]]] = defaultdict(list)
        for job_id, rs in by_job.items():
            ts = rs[0].ts
            test_failed: set[str] = set()
            file_failed: set[str] = set()
            level = rs[0].test_level
            file_state: dict[str, int] = {}
            for r in rs:
                per_unit[r.test_id].append((ts, int(r.failed)))
                if r.granularity == "test":
                    if r.failed:
                        test_failed.add(r.test_id)
                else:
                    if r.failed:
                        file_failed.add(r.test_id)
                    # DEFAULT: per-file outcomes come from file-level rows only. Test-level rows
                    # of passing tests are mostly not stored, so counting them would make every
                    # file look like it always fails.
                    if r.file:
                        file_state[r.file] = max(file_state.get(r.file, 0), int(r.failed))
            runs[job_id] = (ts, frozenset(test_failed or file_failed), level)
            for f, bad in file_state.items():
                per_file[f].append((ts, bad))
        ordered = sorted(runs.items(), key=lambda kv: (kv[1][0], kv[0]))
        self.runs = [(v[1], v[2]) for _, v in ordered]  # (failed ids, test_level)
        self.run_ts = [v[0] for _, v in ordered]
        events = sorted((v[0], tid) for _, v in ordered for tid in v[1])
        self.fail_ts = [e[0] for e in events]
        self.fail_ids = [e[1] for e in events]
        self.per_unit = {k: sorted(v) for k, v in per_unit.items()}
        self.per_file = {k: sorted(v) for k, v in per_file.items()}

    def features(
        self,
        cutoff: float,
        touched_files: Sequence[str] = (),
        settings: FeatureSettings | None = None,  # noqa: ARG002
    ) -> FeatureDict:
        """Feature values from attempts completed strictly before `cutoff`."""
        n = bisect_left(self.run_ts, cutoff)
        out: FeatureDict = dict.fromkeys(FEATURE_NAMES)
        for d in WINDOWS_DAYS:
            out[f"ht_parsed_runs_{d}d"] = n - bisect_left(self.run_ts, cutoff - d * DAY, 0, n)
        if n:
            out["ht_test_level"] = self.runs[n - 1][1]
            last = self.runs[n - 1][0]
            out["ht_last_run_failed_tests"] = len(last)
            out["ht_persistent_failing_tests"] = len(last & self.runs[n - 2][0]) if n >= 2 else 0
        hi = bisect_left(self.fail_ts, cutoff)
        for d in WINDOWS_DAYS:
            lo = bisect_left(self.fail_ts, cutoff - d * DAY, 0, hi)
            failed = set(self.fail_ids[lo:hi])
            out[f"ht_failed_tests_{d}d"] = len(failed)
            out[f"ht_flaky_tests_{d}d"] = self._count_flaky(failed, cutoff - d * DAY, cutoff)
        self._touched(out, cutoff, touched_files)
        return out

    def _count_flaky(self, failed: set[str], lo_ts: float, cutoff: float) -> int:
        """Tests that both failed and passed inside the window (an outcome flip)."""
        count = 0
        for tid in failed:
            ev = self.per_unit[tid]
            a = bisect_left(ev, (lo_ts, -1))
            b = bisect_left(ev, (cutoff, -1))
            window = [f for _, f in ev[a:b]]
            if 1 in window and 0 in window:
                count += 1
        return count

    def _touched(self, out: FeatureDict, cutoff: float, touched: Sequence[str]) -> None:
        """Features of the test files this commit changes (file level; unknown without a file)."""
        files = sorted(set(touched))
        out["ht_touched_test_files"] = len(files)
        if not files:
            return
        fails = runs = with_hist = 0
        flips: list[float] = []
        last_failed: int | None = None
        last_ts = -1.0
        fails_90 = 0
        for f in files:
            ev = self.per_file.get(f)
            if not ev:
                continue
            k = bisect_left(ev, (cutoff, -1))
            if k == 0:
                continue
            with_hist += 1
            prior = ev[:k]
            fails += sum(x for _, x in prior)
            runs += len(prior)
            fails_90 += sum(x for t, x in prior if t >= cutoff - 90 * DAY)
            flips.append(_flip_rate([x for _, x in prior[-FLIP_WINDOW:]]))
            if prior[-1][0] > last_ts:
                last_ts, last_failed = prior[-1][0], prior[-1][1]
            elif prior[-1][0] == last_ts and last_failed is not None:
                last_failed = max(last_failed, prior[-1][1])
        out["ht_touched_files_with_history"] = with_hist
        if runs:
            out["ht_touched_fail_rate"] = fails / runs
            out["ht_touched_max_flip_rate"] = max(flips)
            out["ht_touched_last_failed"] = last_failed
            out["ht_touched_failures_90d"] = fails_90


def _flip_rate(seq: list[int]) -> float:
    """Share of neighbouring outcomes that differ (0.0 for fewer than two outcomes)."""
    if len(seq) < 2:
        return 0.0
    return sum(1 for a, b in zip(seq, seq[1:], strict=False) if a != b) / (len(seq) - 1)


EMPTY_TEST_HISTORY = TestHistory(())
