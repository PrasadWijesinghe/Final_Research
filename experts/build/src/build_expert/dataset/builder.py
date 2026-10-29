"""Build a frozen Build-expert dataset from stored CI history (docs/experts/build.md section 5).

One row per commit (DECISIONS D02): the first attempts of all build-relevant workflow runs on that
head SHA. The cut-off is the earliest of those runs' `created_at`; features use nothing after it.

Steps:
1. Label every failed job with the build rules (and optionally store the FailureLabels).
2. Rebuild each commit's CommitContext by replay and compute feature groups A to C.
3. Collect every commit's outcome, then compute history features (group D) with no look-ahead.
4. Mine candidate fix commits for the knowledge base.
5. Write rows.parquet, fix_candidates.parquet and manifest.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from features.replay import RawHistory, ReplayDataError, build_replay_context

from shared.contracts import FailureLabel
from shared.file_tags import FileTagger

from ..features import (
    CATEGORICAL_FEATURES,
    FEATURE_GROUPS,
    FEATURE_NAMES,
    CommitOutcome,
    HistoryIndex,
    commit_features,
    history_for,
    module_dirs,
)
from ..labels import LABEL_RULES_VERSION, JobFacts, LabelDecision, decide, to_failure_label
from ..parsing import JobSignals, parse_job_log
from ..parsing.signals import PARSER_VERSION
from .source import DatasetSource, JobInfo, RepoInfo, RunInfo

logger = logging.getLogger(__name__)

# DEFAULT: a fix must land on the same branch within 7 days (the Test expert's window, D31).
FIX_WINDOW = timedelta(days=7)
# DEFAULT: history features look back 90 days, so a dataset limited with --from still loads the
# 90 days before it (rows are only written for commits inside the window).
HISTORY_LOOKBACK = timedelta(days=90)
# DEFAULT: at most this many error locations are kept per failure in fix_candidates.
MAX_LOCATIONS = 10
LINT_SUBS = frozenset({"eslint", "format"})
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
SCHEMA_VERSION = "1"


class LabelSink(Protocol):
    """Where rule labels are stored (platform.failure_labels, or a list in tests)."""

    def replace_build_labels(self, job_ids: Sequence[int], labels: Sequence[FailureLabel]) -> int:
        """Drop earlier build rule labels of these jobs, then store the new ones."""
        ...


@dataclass
class ListLabelSink:
    """A LabelSink that keeps labels in memory."""

    labels: list[FailureLabel] = field(default_factory=list)

    def replace_build_labels(self, job_ids: Sequence[int], labels: Sequence[FailureLabel]) -> int:
        """Drop earlier build rule labels of these jobs, then store the new ones."""
        jobs = set(job_ids)
        self.labels = [lb for lb in self.labels if lb.job_id not in jobs] + list(labels)
        return len(labels)


@dataclass
class DatasetSettings:
    """What to include in one dataset build."""

    version: str
    member: str = "build"
    start: datetime | None = None
    end: datetime | None = None
    repos: Sequence[str] | None = None


@dataclass
class DatasetSummary:
    """What a build produced and skipped."""

    out_dir: Path
    rows: int = 0
    positives: int = 0
    fix_candidates: int = 0
    labels_written: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    rows_per_repo: Counter[str] = field(default_factory=Counter)


@dataclass
class _Pending:
    """One commit row before history features are added."""

    row: dict[str, Any]
    outcome: CommitOutcome | None
    repo: str
    cutoff: datetime
    branch: str
    author_hash: str | None
    dirs: frozenset[str]
    default_branch: str | None
    changed_paths: tuple[str, ...]
    locations: list[dict[str, Any]]
    in_window: bool = True


def _git_commit(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=10
        )
    except OSError:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _passed_on_rerun(job: JobInfo, later: Sequence[JobInfo]) -> bool:
    """Did the same job pass in a later attempt of the same run?"""
    for other in later:
        same = (job.job_key and other.job_key == job.job_key) or (
            not job.job_key and other.name == job.name
        )
        if same and other.conclusion == "success":
            return True
    return False


class _SignalCache:
    """Parses each stored log once per build."""

    def __init__(self, source: DatasetSource) -> None:
        self._source = source
        self._cache: dict[int, JobSignals | None] = {}

    def get(self, job: JobInfo) -> JobSignals | None:
        if job.job_id not in self._cache:
            text = (
                self._source.read_log(job.log_path)
                if job.log_path and job.log_status in (None, "ok")
                else None
            )
            self._cache[job.job_id] = parse_job_log(text) if text else None
        return self._cache[job.job_id]


def _label_jobs(
    repo: RepoInfo, jobs_by_attempt: dict[tuple[int, int], list[JobInfo]], cache: _SignalCache
) -> dict[int, tuple[JobInfo, LabelDecision]]:
    """Decide every failed job of every attempt; returns {job_id: (job, decision)}."""
    out: dict[int, tuple[JobInfo, LabelDecision]] = {}
    attempts_of: dict[int, list[int]] = {}
    for run_id, attempt in jobs_by_attempt:
        attempts_of.setdefault(run_id, []).append(attempt)
    for (run_id, attempt), jobs in jobs_by_attempt.items():
        if not any(j.conclusion == "failure" for j in jobs):
            continue
        later = [
            j
            for att in attempts_of[run_id]
            if att > attempt
            for j in jobs_by_attempt[(run_id, att)]
        ]
        for job in jobs:
            if job.conclusion != "failure":
                continue
            facts = JobFacts(
                repo=repo.full_name,
                run_id=run_id,
                attempt=attempt,
                job_id=job.job_id,
                conclusion=job.conclusion,
                name=job.name,
                job_key=job.job_key,
                steps=job.steps,
                passed_on_rerun=_passed_on_rerun(job, later),
            )
            out[job.job_id] = (job, decide(facts, cache.get(job)))
    return out


def _commit_labels(
    first_jobs: Sequence[JobInfo], decisions: dict[int, tuple[JobInfo, LabelDecision]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Commit-level targets from the first-attempt jobs, plus the error locations."""
    claimed = [decisions[j.job_id][1] for j in first_jobs if j.job_id in decisions]
    claimed = [d for d in claimed if d.claimed]
    failed = [j for j in first_jobs if j.conclusion in ("failure", "timed_out", "startup_failure")]
    subs = {d.sub_label for d in claimed}
    best = max((d.confidence for d in claimed), key=_CONFIDENCE_RANK.__getitem__, default=None)
    missing_log = any(
        j.conclusion == "failure"
        and not (decisions.get(j.job_id) and decisions[j.job_id][1].claimed)
        and (j.log_path is None or j.log_status not in (None, "ok"))
        for j in failed
    )
    codes: Counter[str] = Counter()
    locations: list[dict[str, Any]] = []
    for d in claimed:
        for f in d.findings:
            codes.update(f.codes)
            for loc in f.locations[:MAX_LOCATIONS]:
                locations.append(
                    {
                        "tool": f.tool,
                        "path": loc.path,
                        "line": loc.line,
                        "code": loc.code,
                        "message": loc.message[:300],
                    }
                )
    labels = {
        "y_build": int(bool(claimed)),
        "y_build_confident": int(any(d.confidence in ("high", "medium") for d in claimed)),
        "y_tsc": int("tsc" in subs),
        "y_lint": int(bool(subs & LINT_SUBS)),
        "y_bundler": int("bundler" in subs),
        "y_build_other": int("build_other" in subs),
        "y_any_failure": int(bool(failed)),
        "y_other_failure": int(bool(failed) and not claimed),
        "n_failed_jobs": len(failed),
        "n_build_failed_jobs": len(claimed),
        "label_confidence": best,
        "label_rules": ",".join(sorted({d.rule_name for d in claimed})) or None,
        "label_status": "missing_log" if missing_log else "ok",
        "top_error_codes": ",".join(c for c, _ in codes.most_common(5)) or None,
    }
    return labels, locations[:MAX_LOCATIONS]


def _signal_totals(jobs: Sequence[JobInfo], cache: _SignalCache) -> tuple[int | None, int | None]:
    """ESLint warnings and type errors printed by these jobs (None when no log had that tool)."""
    signals = [s for j in jobs if (s := cache.get(j)) is not None]
    lint = [s.lint_warnings for s in signals if "eslint" in s.tools_seen]
    types = [s.type_errors for s in signals if "tsc" in s.tools_seen or s.type_errors]
    return (sum(lint) if lint else None, sum(types) if types else None)


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    raise TypeError(f"numeric feature expected, got {value!r}")


ID_COLUMNS: tuple[tuple[str, pa.DataType], ...] = (
    ("repo", pa.string()),
    ("head_sha", pa.string()),
    ("branch", pa.string()),
    ("event", pa.string()),
    ("pr_number", pa.int64()),
    ("author_hash", pa.string()),
    ("history_cutoff", pa.timestamp("us", tz="UTC")),
    ("completed_at", pa.timestamp("us", tz="UTC")),
    ("n_runs", pa.int64()),
    ("run_ids", pa.string()),
    ("workflow_paths", pa.string()),
    ("licence", pa.string()),
    ("is_synthetic", pa.bool_()),
)
LABEL_COLUMNS: tuple[tuple[str, pa.DataType], ...] = (
    ("y_build", pa.int8()),
    ("y_build_confident", pa.int8()),
    ("y_tsc", pa.int8()),
    ("y_lint", pa.int8()),
    ("y_bundler", pa.int8()),
    ("y_build_other", pa.int8()),
    ("y_any_failure", pa.int8()),
    ("y_other_failure", pa.int8()),
    ("n_failed_jobs", pa.int64()),
    ("n_build_failed_jobs", pa.int64()),
    ("label_confidence", pa.string()),
    ("label_rules", pa.string()),
    ("label_status", pa.string()),
    ("top_error_codes", pa.string()),
)


def rows_schema() -> pa.Schema:
    """The fixed column layout of rows.parquet."""
    feature_fields = [
        pa.field(n, pa.string() if n in CATEGORICAL_FEATURES else pa.float64())
        for n in FEATURE_NAMES
    ]
    return pa.schema(
        [pa.field(n, t) for n, t in ID_COLUMNS]
        + [pa.field(n, t) for n, t in LABEL_COLUMNS]
        + feature_fields
    )


FIX_SCHEMA = pa.schema(
    [
        ("repo", pa.string()),
        ("branch", pa.string()),
        ("failed_sha", pa.string()),
        ("fix_sha", pa.string()),
        ("failed_cutoff", pa.timestamp("us", tz="UTC")),
        ("hours_to_fix", pa.float64()),
        ("sub_labels", pa.string()),
        ("error_codes", pa.string()),
        ("error_locations", pa.string()),  # JSON list of {tool, path, line, code, message}
        ("fix_message", pa.string()),
        ("fix_changed_paths", pa.string()),  # JSON list
        ("overlap_paths", pa.string()),  # JSON list: error paths the fix touched
        ("licence", pa.string()),
        ("origin", pa.string()),
    ]
)


def _write_parquet(rows: list[dict[str, Any]], schema: pa.Schema, path: Path) -> None:
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path, compression="zstd")


def build_dataset(
    source: DatasetSource,
    history: RawHistory,
    out_root: Path,
    settings: DatasetSettings,
    *,
    tagger: FileTagger | None = None,
    label_sink: LabelSink | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    repo_root: Path | None = None,
) -> DatasetSummary:
    """Build `out_root/<version>/` from stored history. Never contacts GitHub."""
    out_dir = out_root / settings.version
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} already exists; datasets are frozen, pick a new version")
    summary = DatasetSummary(out_dir=out_dir)
    pending: list[_Pending] = []
    all_labels: list[FailureLabel] = []
    considered_jobs: list[int] = []
    repo_info: dict[str, RepoInfo] = {}

    load_from = settings.start - HISTORY_LOOKBACK if settings.start else None
    for repo in source.repos(settings.member, settings.repos):
        repo_info[repo.full_name] = repo
        runs = source.runs(repo, load_from, settings.end)
        jobs = source.jobs(runs)
        jobs_by_attempt: dict[tuple[int, int], list[JobInfo]] = {}
        for j in jobs:
            jobs_by_attempt.setdefault((j.run_id, j.attempt), []).append(j)
        cache = _SignalCache(source)
        decisions = _label_jobs(repo, jobs_by_attempt, cache)
        considered_jobs.extend(decisions)
        for job, decision in decisions.values():
            if decision.claimed:
                facts = JobFacts(
                    repo.full_name, job.run_id, job.attempt, job.job_id, job.conclusion
                )
                all_labels.append(to_failure_label(facts, decision))

        by_sha: dict[str, list[RunInfo]] = {}
        for r in runs:
            by_sha.setdefault(r.head_sha, []).append(r)
        for sha, sha_runs in sorted(
            by_sha.items(), key=lambda kv: min(r.created_at for r in kv[1])
        ):
            item = _commit_row(
                repo, sha, sha_runs, jobs_by_attempt, decisions, cache, history, tagger
            )
            in_window = settings.start is None or (
                min(r.created_at for r in sha_runs) >= settings.start
            )
            if isinstance(item, str):
                if in_window:
                    summary.skipped[item] += 1
            else:
                item.in_window = in_window
                pending.append(item)

    index = HistoryIndex(p.outcome for p in pending if p.outcome is not None)
    rows: list[dict[str, Any]] = []
    for p in pending:
        if not p.in_window:
            continue  # loaded only as history for the rows inside the window
        hist = history_for(
            index,
            repo=p.repo,
            cutoff=p.cutoff,
            branch=p.branch,
            author_hash=p.author_hash,
            dirs=p.dirs,
            default_branch=p.default_branch,
        )
        row = dict(p.row)
        for name in FEATURE_NAMES:
            value = row.get(name, hist.get(name))
            row[name] = value if name in CATEGORICAL_FEATURES else _to_float(value)
        rows.append(row)
        summary.rows_per_repo[p.repo] += 1

    fixes = _fix_candidates(pending, source, repo_info)
    if label_sink is not None and considered_jobs:
        summary.labels_written = label_sink.replace_build_labels(considered_jobs, all_labels)

    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path, fixes_path = out_dir / "rows.parquet", out_dir / "fix_candidates.parquet"
    _write_parquet(rows, rows_schema(), rows_path)
    _write_parquet(fixes, FIX_SCHEMA, fixes_path)
    summary.rows, summary.fix_candidates = len(rows), len(fixes)
    summary.positives = sum(r["y_build"] for r in rows)
    manifest = _manifest(settings, summary, rows, all_labels, clock(), repo_root, repo_info)
    manifest["files"] = {
        p.name: {"sha256": _sha256(p), "bytes": p.stat().st_size} for p in (rows_path, fixes_path)
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    logger.info(
        "dataset written",
        extra={"out_dir": str(out_dir), "rows": summary.rows, "positives": summary.positives},
    )
    return summary


def _commit_row(
    repo: RepoInfo,
    sha: str,
    sha_runs: Sequence[RunInfo],
    jobs_by_attempt: dict[tuple[int, int], list[JobInfo]],
    decisions: dict[int, tuple[JobInfo, LabelDecision]],
    cache: _SignalCache,
    history: RawHistory,
    tagger: FileTagger | None,
) -> _Pending | str:
    """One commit's row (without history), or the reason it was skipped."""
    first = [r for r in sha_runs if r.attempt == 1]
    if not first:
        return "no_first_attempt"
    if any(r.status != "completed" for r in first):
        return "run_not_completed"
    if all(r.conclusion in ("cancelled", "skipped") for r in first):
        return "all_runs_cancelled"
    anchor = min(first, key=lambda r: (r.created_at, r.run_id))
    first_jobs = [j for r in first for j in jobs_by_attempt.get((r.run_id, 1), [])]
    if not first_jobs:
        return "no_jobs_stored"
    try:
        ctx = build_replay_context(
            history, repo=repo.full_name, run_id=anchor.run_id, attempt=1, tagger=tagger
        )
    except ReplayDataError as exc:
        logger.info("replay skipped", extra={"repo": repo.full_name, "sha": sha, "err": str(exc)})
        return "replay_data_missing"
    labels, locations = _commit_labels(first_jobs, decisions)
    completed = max((r.updated_at or r.created_at) for r in first)
    dirs = module_dirs(ctx.changed_files)
    claimed_subs = frozenset(
        d.sub_label or "build_other"
        for j in first_jobs
        if j.job_id in decisions and (d := decisions[j.job_id][1]).claimed
    )
    lint_warnings, type_errors = _signal_totals(first_jobs, cache)
    outcome = CommitOutcome(
        repo=repo.full_name,
        sha=sha,
        branch=ctx.branch,
        author_hash=ctx.author_hash,
        completed_at=completed,
        is_default_branch=ctx.branch == repo.default_branch,
        build_failed=bool(labels["y_build"]),
        sub_labels=claimed_subs,
        any_failed=bool(labels["y_any_failure"]),
        dirs=dirs,
        lint_warnings=lint_warnings,
        type_errors=type_errors,
    )
    row: dict[str, Any] = {
        "repo": repo.full_name,
        "head_sha": sha,
        "branch": ctx.branch,
        "event": anchor.event,
        "pr_number": ctx.pr_number,
        "author_hash": ctx.author_hash,
        "history_cutoff": ctx.history_cutoff,
        "completed_at": completed,
        "n_runs": len(first),
        "run_ids": ",".join(str(r.run_id) for r in sorted(first, key=lambda r: r.run_id)),
        "workflow_paths": ",".join(sorted({r.workflow_path or "" for r in first})),
        "licence": repo.licence,
        "is_synthetic": ctx.is_synthetic,
        **labels,
        **commit_features(ctx, default_branch=repo.default_branch),
    }
    return _Pending(
        row=row,
        outcome=outcome,
        repo=repo.full_name,
        cutoff=ctx.history_cutoff,
        branch=ctx.branch,
        author_hash=ctx.author_hash,
        dirs=dirs,
        default_branch=repo.default_branch,
        changed_paths=tuple(f.path for f in ctx.changed_files),
        locations=locations,
    )


def _fix_candidates(
    pending: Sequence[_Pending], source: DatasetSource, repos: dict[str, RepoInfo]
) -> list[dict[str, Any]]:
    """A build failure followed, on the same branch within 7 days, by a fully green commit."""
    by_branch: dict[tuple[str, str], list[_Pending]] = {}
    for p in pending:
        by_branch.setdefault((p.repo, p.branch), []).append(p)
    out: list[dict[str, Any]] = []
    for (repo, branch), items in by_branch.items():
        items = sorted(items, key=lambda p: p.cutoff)
        for i, failed in enumerate(items):
            if not failed.row["y_build"] or not failed.in_window:
                continue
            for nxt in items[i + 1 :]:
                if nxt.cutoff - failed.cutoff > FIX_WINDOW:
                    break
                if nxt.row["y_build"]:
                    continue  # still failing; keep looking for the first green commit
                if nxt.row["y_any_failure"]:
                    break  # green for build but red for something else: not a clean fix
                error_paths = {loc["path"] for loc in failed.locations if loc.get("path")}
                out.append(
                    {
                        "repo": repo,
                        "branch": branch,
                        "failed_sha": failed.row["head_sha"],
                        "fix_sha": nxt.row["head_sha"],
                        "failed_cutoff": failed.cutoff,
                        "hours_to_fix": (nxt.cutoff - failed.cutoff).total_seconds() / 3600,
                        "sub_labels": ",".join(
                            s
                            for s, col in (
                                ("tsc", "y_tsc"),
                                ("lint", "y_lint"),
                                ("bundler", "y_bundler"),
                                ("build_other", "y_build_other"),
                            )
                            if failed.row[col]
                        ),
                        "error_codes": failed.row["top_error_codes"],
                        "error_locations": json.dumps(failed.locations),
                        "fix_message": source.commit_message(repos[repo], nxt.row["head_sha"]),
                        "fix_changed_paths": json.dumps(list(nxt.changed_paths)),
                        "overlap_paths": json.dumps(sorted(error_paths & set(nxt.changed_paths))),
                        "licence": failed.row["licence"],
                        "origin": "mined_candidate",
                    }
                )
                break
    return out


def _manifest(
    settings: DatasetSettings,
    summary: DatasetSummary,
    rows: Sequence[dict[str, Any]],
    labels: Sequence[FailureLabel],
    built_at: datetime,
    repo_root: Path | None,
    repos: dict[str, RepoInfo],
) -> dict[str, Any]:
    cutoffs = [r["history_cutoff"] for r in rows]
    per_repo = {
        name: {
            "rows": summary.rows_per_repo.get(name, 0),
            "y_build": sum(r["y_build"] for r in rows if r["repo"] == name),
            "licence": info.licence,
        }
        for name, info in sorted(repos.items())
    }
    count = Counter
    return {
        "dataset": f"build/{settings.version}",
        "schema_version": SCHEMA_VERSION,
        "built_at": built_at.isoformat(),
        "git_commit": _git_commit(repo_root) if repo_root else None,
        "member": settings.member,
        "unit": "commit: first attempts of the build-relevant workflow runs on the head SHA",
        "window": {
            "from": settings.start.isoformat() if settings.start else None,
            "to": settings.end.isoformat() if settings.end else None,
        },
        "history_cutoff": {
            "min": min(cutoffs).isoformat() if cutoffs else None,
            "max": max(cutoffs).isoformat() if cutoffs else None,
        },
        "repos": per_repo,
        "rows": summary.rows,
        "label_counts": {
            "y_build": dict(count(str(r["y_build"]) for r in rows)),
            "y_build_confident": dict(count(str(r["y_build_confident"]) for r in rows)),
            "y_tsc": sum(r["y_tsc"] for r in rows),
            "y_lint": sum(r["y_lint"] for r in rows),
            "y_bundler": sum(r["y_bundler"] for r in rows),
            "y_build_other": sum(r["y_build_other"] for r in rows),
            "y_other_failure": sum(r["y_other_failure"] for r in rows),
            "label_confidence": dict(count(str(r["label_confidence"]) for r in rows)),
            "label_status": dict(count(r["label_status"] for r in rows)),
        },
        "job_labels": {
            "written": summary.labels_written,
            "claimed": len(labels),
            "by_rule": dict(count(lb.rule_name or "" for lb in labels)),
            "by_sub_label": dict(count(lb.sub_label or "" for lb in labels)),
        },
        "skipped_commits": dict(summary.skipped),
        "fix_candidates": summary.fix_candidates,
        "synthetic_rows": sum(1 for r in rows if r["is_synthetic"]),
        "versions": {"label_rules": LABEL_RULES_VERSION, "parser": PARSER_VERSION},
        "features": {group: list(names) for group, names in FEATURE_GROUPS.items()},
        "categorical_features": list(CATEGORICAL_FEATURES),
    }
