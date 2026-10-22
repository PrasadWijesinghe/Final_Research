"""Replay CLI (`uv run replay --member test --from 2026-01-01 --to 2026-01-31`).

For each stored run in the window it rebuilds the CommitContext from the raw tables
(`features.replay`), asks the chosen experts for predictions, stores them with `is_replay=true`,
and routes failed jobs (stored with `replay=true`, then queued for the expert workers).

Replay NEVER touches GitHub: this module and its imports never construct a GitHub client (a test
checks the import graph and that no HTTP request reaches github.com).

# DEFAULT: the CLI lives in services/api (not services/features) because features is a library
# that api imports; a replay CLI in features would have to import api (fan-out, router, store),
# which would be circular.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from features.assemble import parse_repo
from features.replay import RawHistory, ReplayDataError, RunRow, SqlHistory, build_replay_context
from redis import Redis
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from shared.contracts import Expert, RiskOutput
from shared.contracts.routed_failure import FailedStep
from shared.db import models as m

from .config import Settings
from .experts import fan_out_predict
from .failure_queue import redis_from_env
from .router import FailedJob, RouterRules, load_rules, route_failed_job
from .store import SqlStore, Store

__all__ = [
    "InMemoryReplaySource",
    "ReplayRun",
    "ReplaySource",
    "ReplaySummary",
    "SqlReplaySource",
    "experts_for_member",
    "main",
    "replay_window",
]

logger = logging.getLogger(__name__)

# DEFAULT: the last 20,000 characters of the stored log are the excerpt (the contract's maximum).
EXCERPT_CHARS = 20_000
_FAILED_STEP = ("failure", "cancelled", "timed_out")


@dataclass(frozen=True)
class ReplayRun:
    """A stored run chosen for replay."""

    repo: str  # "owner/name"
    run: RunRow


@dataclass
class ReplaySummary:
    """What a replay run did."""

    runs: int = 0
    skipped_runs: int = 0
    predictions: int = 0
    experts_not_ready: int = 0
    failures_routed: int = 0
    failures_unrouted: int = 0
    errors: list[str] = field(default_factory=list)


class ReplaySource(Protocol):
    """Lists what to replay from the raw tables (runs in a window, failed jobs of a run)."""

    def runs_between(self, start: datetime, end: datetime, member: str | None) -> list[ReplayRun]:
        """Runs created in [start, end), oldest first; `member` limits them to its repos."""
        ...

    def failed_jobs(self, run: ReplayRun) -> list[FailedJob]:
        """Failed jobs of the run that have a stored log; `replay=True`, no context id yet."""
        ...


def experts_for_member(member: str) -> list[Expert]:
    """Experts to call for a member name."""
    # DEFAULT: a member's name equals its expert's value (config/repos/<expert>.yaml, MEMBER env);
    # `all` calls all four experts.
    if member == "all":
        return list(Expert)
    return [Expert(member)]


class SqlReplaySource:
    """`ReplaySource` over the `raw` schema; logs are read from `data_dir`."""

    def __init__(self, engine: Engine, data_dir: Path) -> None:
        self._engine = engine
        self._data_dir = data_dir

    def runs_between(self, start: datetime, end: datetime, member: str | None) -> list[ReplayRun]:
        """Runs created in [start, end), oldest first."""
        stmt = (
            select(m.WorkflowRun, m.Repo.full_name)
            .join(m.Repo, m.Repo.id == m.WorkflowRun.repo_id)
            .where(m.WorkflowRun.created_at >= start, m.WorkflowRun.created_at < end)
            .order_by(m.WorkflowRun.created_at, m.WorkflowRun.run_id)
        )
        if member:
            stmt = stmt.where(m.Repo.member == member)
        with Session(self._engine) as s:
            return [
                ReplayRun(
                    full_name,
                    RunRow(
                        run_id=row.run_id,
                        attempt=row.attempt,
                        head_sha=row.head_sha,
                        created_at=row.created_at,  # type: ignore[arg-type]  # filtered non-null
                        workflow_path=row.workflow_path,
                        head_branch=row.head_branch,
                        actor_hash=row.actor_hash,
                        pr_numbers=tuple(row.pr_numbers or ()),
                    ),
                )
                for row, full_name in s.execute(stmt).all()
            ]

    def failed_jobs(self, run: ReplayRun) -> list[FailedJob]:
        """Failed jobs of the run with a stored log."""
        jobs: list[FailedJob] = []
        if not run.run.workflow_path:
            return jobs
        with Session(self._engine) as s:
            rows = s.scalars(
                select(m.Job)
                .where(
                    m.Job.run_id == run.run.run_id,
                    m.Job.attempt == run.run.attempt,
                    m.Job.conclusion == "failure",
                )
                .order_by(m.Job.job_id)
            ).all()
            for job in rows:
                steps = s.scalars(
                    select(m.Step)
                    .where(m.Step.job_id == job.job_id, m.Step.conclusion.in_(_FAILED_STEP))
                    .order_by(m.Step.number)
                ).all()
                log = s.get(m.Log, job.job_id)
                excerpt = self._read_log(log.storage_path) if log and log.status == "ok" else None
                if not steps or log is None or not log.storage_path or excerpt is None:
                    logger.info(
                        "job skipped: no failed step or stored log", extra={"job_id": job.job_id}
                    )
                    continue
                jobs.append(
                    FailedJob(
                        repo=parse_repo(run.repo),
                        head_sha=run.run.head_sha,
                        run_id=run.run.run_id,
                        run_attempt=run.run.attempt,
                        job_id=job.job_id,
                        job_name=job.name or str(job.job_id),
                        workflow_path=run.run.workflow_path,
                        job_key=job.job_key,
                        failed_steps=[
                            FailedStep(
                                number=st.number,
                                name=st.name or f"step {st.number}",
                                conclusion=st.conclusion,  # type: ignore[arg-type]
                            )
                            for st in steps
                        ],
                        log_ref=log.storage_path,
                        log_excerpt=excerpt,
                        replay=True,
                    )
                )
        return jobs

    def _read_log(self, storage_path: str | None) -> str | None:
        if not storage_path:
            return None
        try:
            data = gzip.decompress((self._data_dir / storage_path).read_bytes())
        except OSError:
            return None
        return data.decode("utf-8", errors="replace")[-EXCERPT_CHARS:]


@dataclass
class InMemoryReplaySource:
    """A `ReplaySource` over lists, for tests."""

    runs: list[ReplayRun] = field(default_factory=list)
    jobs: dict[tuple[str, int, int], list[FailedJob]] = field(default_factory=dict)

    def runs_between(self, start: datetime, end: datetime, member: str | None) -> list[ReplayRun]:
        """Runs created in [start, end), oldest first (`member` is ignored here)."""
        found = [r for r in self.runs if start <= r.run.created_at < end]
        return sorted(found, key=lambda r: (r.run.created_at, r.run.run_id))

    def failed_jobs(self, run: ReplayRun) -> list[FailedJob]:
        """Failed jobs registered for the run."""
        key = (run.repo, run.run.run_id, run.run.attempt)
        return [j.model_copy() for j in self.jobs.get(key, [])]


async def replay_window(
    *,
    source: ReplaySource,
    history: RawHistory,
    store: Store,
    settings: Settings,
    connection: Redis,
    experts: Sequence[Expert],
    start: datetime,
    end: datetime,
    member: str | None = None,
    rules: RouterRules | None = None,
) -> ReplaySummary:
    """Replay every stored run created in [start, end); one bad run never stops the others."""
    rules = rules or load_rules()  # preloaded once, not per failure
    summary = ReplaySummary()
    for item in source.runs_between(start, end, member):
        try:
            context = build_replay_context(
                history, repo=item.repo, run_id=item.run.run_id, attempt=item.run.attempt
            )
        except ReplayDataError as exc:
            summary.skipped_runs += 1
            summary.errors.append(str(exc))
            logger.warning("run skipped", extra={"run_id": item.run.run_id, "reason": str(exc)})
            continue
        summary.runs += 1
        store.save_context(context)
        # DEFAULT: replay uses the PR-notes budget, not the 10 s pre-push one (nobody is waiting).
        results = await fan_out_predict(settings, context, settings.pr_budget_ms, experts)
        risks: list[RiskOutput] = []
        for r in results:
            if r.state == "ready":
                risks.extend(r.risks)
            else:
                summary.experts_not_ready += 1
        if risks:
            store.save_predictions(risks, is_replay=True)
            summary.predictions += len(risks)
        for job in source.failed_jobs(item):
            result = route_failed_job(
                job.model_copy(update={"context_id": context.context_id, "replay": True}),
                store,
                connection,
                rules,
            )
            if result.failure is None:
                summary.failures_unrouted += 1
            else:
                summary.failures_routed += 1
    return summary


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from exc


def build_parser() -> argparse.ArgumentParser:
    """Command line arguments."""
    p = argparse.ArgumentParser(
        prog="replay", description="Replay stored runs through the experts (never calls GitHub)."
    )
    p.add_argument("--member", required=True, choices=[*(e.value for e in Expert), "all"])
    p.add_argument("--from", dest="start", required=True, type=_day, help="first day (UTC)")
    p.add_argument("--to", dest="end", required=True, type=_day, help="last day (UTC, inclusive)")
    p.add_argument("--data-dir", type=Path, help="where logs are stored (default: DATA_DIR)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of `uv run replay`."""
    args = build_parser().parse_args(argv)
    if args.end < args.start:
        sys.stderr.write("--to must not be before --from\n")
        return 2
    from shared.db.session import get_engine  # here so `--help` needs no database settings

    engine = get_engine()
    # DEFAULT: logs live in `./data` unless DATA_DIR says otherwise (run from the repo root).
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or Path.cwd() / "data")
    start = datetime(args.start.year, args.start.month, args.start.day, tzinfo=UTC)
    end = datetime(args.end.year, args.end.month, args.end.day, tzinfo=UTC) + timedelta(days=1)
    summary = asyncio.run(
        replay_window(
            source=SqlReplaySource(engine, data_dir),
            history=SqlHistory(engine),
            store=SqlStore(engine),
            settings=Settings(),
            connection=redis_from_env(),
            experts=experts_for_member(args.member),
            start=start,
            end=end,
            member=None if args.member == "all" else args.member,
        )
    )
    sys.stdout.write(
        f"replayed {summary.runs} runs ({summary.skipped_runs} skipped): "
        f"{summary.predictions} predictions, {summary.failures_routed} failures routed, "
        f"{summary.failures_unrouted} unrouted, "
        f"{summary.experts_not_ready} expert answers missing\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
