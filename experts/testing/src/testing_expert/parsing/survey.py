"""Runs the log parsers over a sample of real failed-job logs and reports how well they did.

Run from the repository root with the database settings loaded from `.env` (the file is never
printed):

    uv run --env-file .env python -m testing_expert.parsing.survey
    uv run --env-file .env python -m testing_expert.parsing.survey --n 500 --seed 7

It reads job ids and log paths from the local collector database (every query runs in a
read-only transaction) and the log files from `data/logs/`. Nothing is written. It is not part
of `make check`: it needs the local database and a few thousand logs.

The report has: counts per detected framework, parser exceptions (must be 0), the share of failed
logs whose summary reports failures that yield at least one failing name (target 95%), the
`parsed_ok` and confidence distributions, and the worst misses. Misses are listed by job id and
repo only, never with log text.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from testing_expert.parsing.make_fixtures import _connect_read_only, default_data_dir
from testing_expert.parsing.models import ParsedLog
from testing_expert.parsing.parse import parse_log, reports_failures

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TARGET_MISSED = 1
# DEFAULT: ~2,000 logs is a few minutes of work and covers every repo several times over.
DEFAULT_SAMPLE = 2000
# DEFAULT: seed 42, like the fixture tooling, so a re-run reports on the same logs.
DEFAULT_SEED = 42
# DEFAULT: the 10 worst misses are printed (job id and repo only).
DEFAULT_WORST = 10
# The plan's target: of the failed logs whose summary reports failures, this share must yield at
# least one failing name.
TARGET_SHARE = 0.95
_FAILED_LOGS_SQL = """
select j.job_id, r.full_name, l.storage_path
from raw.logs l
join raw.jobs j on j.job_id = l.job_id
join raw.workflow_runs w on w.run_id = j.run_id and w.attempt = j.attempt
join raw.repos r on r.id = w.repo_id
where l.status = 'ok' and j.conclusion = 'failure'
order by j.job_id
"""
_ERROR_MARKERS = ("could not be parsed", "parser failed")


@dataclass(frozen=True)
class LogRef:
    """A failed job's log: its id, repo and path under the data folder."""

    job_id: int
    repo: str
    storage_path: str


@dataclass
class Outcome:
    """What one log produced, reduced to what the report needs (no log text)."""

    ref: LogRef
    result: ParsedLog
    seconds: float

    @property
    def reported(self) -> int:
        """How many failures the summary reported (failed tests plus failed files)."""
        s = self.result.summary
        return 0 if s is None else (s.failed or 0) + (s.files_failed or 0)

    @property
    def names(self) -> int:
        """How many failing tests or suites were extracted."""
        return sum(1 for f in self.result.failures if f.kind in ("test", "suite"))

    @property
    def excepted(self) -> bool:
        """True when `parse_log` had to catch an exception."""
        return any(m in w for w in self.result.warnings for m in _ERROR_MARKERS)


# DEFAULT: stratify by repo with an equal share per repo; a repo with fewer logs than its share
# gives them all and the shares of the bigger repos grow by the leftover, so no repo dominates.
def sample_stratified(refs: Sequence[LogRef], n: int, seed: int) -> list[LogRef]:
    """Pick up to `n` logs so that no repo dominates: every repo gets an equal share, and the
    share a small repo cannot use is spread over the bigger ones. Seeded, so repeatable."""
    by_repo: dict[str, list[LogRef]] = defaultdict(list)
    for ref in refs:
        by_repo[ref.repo].append(ref)
    rng = random.Random(seed)
    for items in by_repo.values():
        rng.shuffle(items)
    quotas = {repo: 0 for repo in by_repo}
    remaining = min(n, len(refs))
    open_repos = sorted(by_repo)
    while remaining > 0 and open_repos:
        share = max(remaining // len(open_repos), 1)
        still_open: list[str] = []
        for repo in open_repos:
            take = min(share, len(by_repo[repo]) - quotas[repo], remaining)
            quotas[repo] += take
            remaining -= take
            if quotas[repo] < len(by_repo[repo]):
                still_open.append(repo)
        open_repos = still_open
    picked = [ref for repo in sorted(by_repo) for ref in by_repo[repo][: quotas[repo]]]
    rng.shuffle(picked)
    return picked


def fetch_failed_logs() -> list[LogRef]:
    """Every failed job that has a stored log, read-only."""
    from sqlalchemy import text

    with _connect_read_only() as conn:
        rows = conn.execute(text(_FAILED_LOGS_SQL)).all()
    return [LogRef(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def run_survey(refs: Sequence[LogRef], data_dir: Path) -> tuple[list[Outcome], list[LogRef]]:
    """Parse each log. Returns (outcomes, refs whose log file is missing)."""
    outcomes: list[Outcome] = []
    missing: list[LogRef] = []
    for ref in refs:
        path = data_dir / ref.storage_path
        if not path.is_file():
            missing.append(ref)
            continue
        started = time.perf_counter()
        result = parse_log(path)
        outcomes.append(Outcome(ref, result, time.perf_counter() - started))
    return outcomes, missing


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


def share_with_names(outcomes: Sequence[Outcome]) -> tuple[int, int]:
    """(logs whose summary reports failures and yield a failing name, logs that report failures)."""
    reporting = [o for o in outcomes if o.result.summary and reports_failures([o.result.summary])]
    return sum(1 for o in reporting if o.names > 0), len(reporting)


def worst_misses(outcomes: Sequence[Outcome], count: int) -> list[tuple[Outcome, str]]:
    """The logs that miss most: a summary reports failures but no name was found (largest first),
    then logs where the found count differs most from the reported one."""
    misses = sorted(
        (o for o in outcomes if o.reported > 0 and o.names == 0),
        key=lambda o: (-o.reported, o.ref.job_id),
    )
    chosen: list[tuple[Outcome, str]] = [(o, "no failing name found") for o in misses[:count]]
    if len(chosen) < count:
        off = sorted(
            (o for o in outcomes if o.reported > 0 and o.names > 0 and o.result.confidence < 1.0),
            key=lambda o: (-abs(o.reported - o.names), o.ref.job_id),
        )
        chosen += [(o, "found count differs from the summary") for o in off[: count - len(chosen)]]
    return chosen


def format_report(
    outcomes: Sequence[Outcome], missing: Sequence[LogRef], total_failed: int, worst: int
) -> str:
    """The survey report as text."""
    lines: list[str] = []
    n = len(outcomes)
    lines.append(f"Survey of {n} failed-job logs (from {total_failed} available)")
    if missing:
        lines.append(f"Missing log files (skipped): {len(missing)}")
    seconds = sum(o.seconds for o in outcomes)
    slowest = max(outcomes, key=lambda o: o.seconds, default=None)
    lines.append(
        f"Parse time: {seconds:.1f}s total"
        + (f", slowest {slowest.seconds:.2f}s (job {slowest.ref.job_id})" if slowest else "")
    )
    lines.append("")
    lines.append("Detected framework")
    for fw, count in sorted(Counter(o.result.framework for o in outcomes).items()):
        lines.append(f"  {fw:<8}{count:>6}  {_pct(count, n)}")
    lines.append("")
    excepted = [o for o in outcomes if o.excepted]
    lines.append(f"Exceptions caught by parse_log: {len(excepted)} (must be 0)")
    for o in excepted[:worst]:
        lines.append(f"  job {o.ref.job_id}  {o.ref.repo}")
    with_names, reporting = share_with_names(outcomes)
    lines.append("")
    lines.append(
        f"Failed logs whose summary reports failures: {reporting}; "
        f"with at least one failing name: {with_names} ({_pct(with_names, reporting)}, "
        f"target {TARGET_SHARE:.0%})"
    )
    exact = sum(
        1
        for o in outcomes
        if o.result.summary and reports_failures([o.result.summary]) and o.result.confidence == 1.0
    )
    lines.append(
        f"  of those, the failures found agree with every summary (confidence 1.0): "
        f"{exact} ({_pct(exact, reporting)})"
    )
    ok = Counter(o.result.parsed_ok for o in outcomes)
    lines.append(f"parsed_ok: true {ok[True]} ({_pct(ok[True], n)}), false {ok[False]}")
    lines.append("Confidence")
    conf = Counter(o.result.confidence for o in outcomes)
    for value in sorted(conf, reverse=True):
        lines.append(f"  {value:<4}{conf[value]:>6}  {_pct(conf[value], n)}")
    gran = Counter(o.result.granularity for o in outcomes)
    lines.append("Granularity: " + ", ".join(f"{k} {v}" for k, v in sorted(gran.items())))
    lines.append("")
    lines.append(f"Worst misses (up to {worst}; job id and repo only)")
    for o, why in worst_misses(outcomes, worst):
        lines.append(
            f"  job {o.ref.job_id}  {o.ref.repo}  framework={o.result.framework}  "
            f"reported={o.reported} found={o.names}  ({why})"
        )
    if not worst_misses(outcomes, 1):
        lines.append("  none")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m testing_expert.parsing.survey",
        description="Parse a sample of failed-job logs and report how many yield failing names.",
    )
    p.add_argument("--n", type=int, default=DEFAULT_SAMPLE, help="logs to sample (default 2000)")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="sampling seed (default 42)")
    p.add_argument("--worst", type=int, default=DEFAULT_WORST, help="misses to list (default 10)")
    p.add_argument(
        "--data-dir", type=Path, help="collector data folder (default: DATA_DIR or ./data)"
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit code 1 when an exception was caught or the 95% target is missed."""
    args: Any = _parser().parse_args(argv)
    if not os.environ.get("DATABASE_URL", "").strip():
        sys.stderr.write(
            "survey: DATABASE_URL is not set. Run with `uv run --env-file .env python -m "
            "testing_expert.parsing.survey`.\n"
        )
        return EXIT_USAGE
    data_dir = args.data_dir or default_data_dir()
    available = fetch_failed_logs()
    sample = sample_stratified(available, args.n, args.seed)
    outcomes, missing = run_survey(sample, data_dir)
    sys.stdout.write(format_report(outcomes, missing, len(available), args.worst) + "\n")
    with_names, reporting = share_with_names(outcomes)
    missed = reporting > 0 and with_names / reporting < TARGET_SHARE
    return EXIT_TARGET_MISSED if missed or any(o.excepted for o in outcomes) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
