"""Feature group D: what happened to earlier commits (docs/experts/build.md section 4).

No look-ahead: a commit's outcome counts only from the moment its CI runs finished. So history for
a commit with cut-off T uses only outcomes whose `completed_at` is strictly before T.

The "last warnings" features stand in for the proposal's idea of running the linter and compiler
locally (not possible here: the team uses no repo clones, DECISIONS D08). They read the ESLint
warning and TypeScript error counts printed by the most recent earlier run on the same branch.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

# DEFAULT: windows for the rolling rates. 7/30/90 days match the Test expert's history windows.
WINDOWS_DAYS = (7, 30, 90)


@dataclass(frozen=True)
class CommitOutcome:
    """The known result of one earlier commit's CI (first attempts of build-relevant runs)."""

    repo: str
    sha: str
    branch: str
    author_hash: str | None
    completed_at: datetime  # when its last first-attempt run finished: known from then on
    is_default_branch: bool
    build_failed: bool
    sub_labels: frozenset[str] = frozenset()
    any_failed: bool = False
    dirs: frozenset[str] = frozenset()
    lint_warnings: int | None = None  # None when no log with ESLint output was stored
    type_errors: int | None = None


class _Series:
    """Outcomes sorted by completion time, with their times for bisecting."""

    def __init__(self, rows: list[CommitOutcome]) -> None:
        self.rows = sorted(rows, key=lambda o: o.completed_at)
        self.times = [o.completed_at for o in self.rows]

    def window(self, cutoff: datetime, days: int | None) -> Sequence[CommitOutcome]:
        end = bisect.bisect_left(self.times, cutoff)
        start = 0 if days is None else bisect.bisect_left(self.times, cutoff - timedelta(days=days))
        return self.rows[start:end]


_EMPTY = _Series([])


class HistoryIndex:
    """Outcomes per repo, per branch and per author, sorted by completion time, with fast
    "everything finished in [cutoff - days, cutoff)" slices."""

    def __init__(self, outcomes: Iterable[CommitOutcome]) -> None:
        by_repo: dict[str, list[CommitOutcome]] = {}
        by_branch: dict[tuple[str, str], list[CommitOutcome]] = {}
        by_author: dict[tuple[str, str], list[CommitOutcome]] = {}
        for o in outcomes:
            by_repo.setdefault(o.repo, []).append(o)
            by_branch.setdefault((o.repo, o.branch), []).append(o)
            if o.author_hash:
                by_author.setdefault((o.repo, o.author_hash), []).append(o)
        self._repo = {k: _Series(v) for k, v in by_repo.items()}
        self._branch = {k: _Series(v) for k, v in by_branch.items()}
        self._author = {k: _Series(v) for k, v in by_author.items()}

    def window(
        self, repo: str, cutoff: datetime, days: int | None = None
    ) -> Sequence[CommitOutcome]:
        """Repo outcomes completed in [cutoff - days, cutoff); all earlier when days is None."""
        return self._repo.get(repo, _EMPTY).window(cutoff, days)

    def branch_window(
        self, repo: str, branch: str, cutoff: datetime, days: int | None = None
    ) -> Sequence[CommitOutcome]:
        """The same, for one branch."""
        return self._branch.get((repo, branch), _EMPTY).window(cutoff, days)

    def author_window(
        self, repo: str, author_hash: str | None, cutoff: datetime, days: int | None = None
    ) -> Sequence[CommitOutcome]:
        """The same, for one author (empty when the author is unknown)."""
        if not author_hash:
            return ()
        return self._author.get((repo, author_hash), _EMPTY).window(cutoff, days)


NAMES: tuple[str, ...] = (
    *(f"repo_build_fail_rate_{d}d" for d in WINDOWS_DAYS),
    "repo_commits_30d",
    "repo_tsc_fail_rate_30d",
    "repo_lint_fail_rate_30d",
    "repo_bundler_fail_rate_30d",
    "repo_any_fail_rate_30d",
    "branch_has_history",
    "branch_last_build_failed",
    "branch_consecutive_build_fails",
    "hours_since_branch_build_fail",
    "author_commits_90d",
    "author_build_fail_rate_90d",
    "author_last_build_failed",
    "dirs_commits_30d",
    "dirs_build_fail_rate_30d",
    "last_lint_warnings",
    "last_type_errors",
    "lint_warnings_trend",
)

_LINT_SUBS = frozenset({"eslint", "format", "lint_other"})


def _rate(rows: Sequence[CommitOutcome]) -> float | None:
    if not rows:
        return None
    return sum(1 for o in rows if o.build_failed) / len(rows)


def _share(rows: Sequence[CommitOutcome], subs: frozenset[str]) -> float | None:
    if not rows:
        return None
    return sum(1 for o in rows if o.sub_labels & subs) / len(rows)


def history_features(
    index: HistoryIndex,
    *,
    repo: str,
    cutoff: datetime,
    branch: str,
    author_hash: str | None,
    dirs: frozenset[str],
    default_branch: str | None,
) -> dict[str, object]:
    """Group D features for a commit whose cut-off is `cutoff`."""
    feats: dict[str, object] = {}
    for d in WINDOWS_DAYS:
        feats[f"repo_build_fail_rate_{d}d"] = _rate(index.window(repo, cutoff, d))
    last30 = index.window(repo, cutoff, 30)
    feats["repo_commits_30d"] = len(last30)
    feats["repo_tsc_fail_rate_30d"] = _share(last30, frozenset({"tsc"}))
    feats["repo_lint_fail_rate_30d"] = _share(last30, _LINT_SUBS)
    feats["repo_bundler_fail_rate_30d"] = _share(last30, frozenset({"bundler"}))
    feats["repo_any_fail_rate_30d"] = (
        sum(1 for o in last30 if o.any_failed) / len(last30) if last30 else None
    )

    # Same branch; a new pull-request branch falls back to the default branch.
    on_branch = index.branch_window(repo, branch, cutoff, 90)
    feats["branch_has_history"] = bool(on_branch)
    if not on_branch and default_branch:
        on_branch = index.branch_window(repo, default_branch, cutoff, 90)
    if on_branch:
        feats["branch_last_build_failed"] = on_branch[-1].build_failed
        streak = 0
        for o in reversed(on_branch):
            if not o.build_failed:
                break
            streak += 1
        feats["branch_consecutive_build_fails"] = streak
        fails = [o for o in on_branch if o.build_failed]
        feats["hours_since_branch_build_fail"] = (
            (cutoff - fails[-1].completed_at).total_seconds() / 3600 if fails else None
        )
    else:
        feats["branch_last_build_failed"] = None
        feats["branch_consecutive_build_fails"] = 0
        feats["hours_since_branch_build_fail"] = None

    mine = index.author_window(repo, author_hash, cutoff, 90)
    feats["author_commits_90d"] = len(mine)
    feats["author_build_fail_rate_90d"] = _rate(mine)
    feats["author_last_build_failed"] = mine[-1].build_failed if mine else None

    near = [o for o in last30 if o.dirs & dirs]
    feats["dirs_commits_30d"] = len(near)
    feats["dirs_build_fail_rate_30d"] = _rate(near)

    # DEFAULT: look at the 20 latest outcomes on the branch for warning counts.
    recent = on_branch[-20:]
    with_lint = [o for o in recent if o.lint_warnings is not None]
    with_types = [o for o in recent if o.type_errors is not None]
    feats["last_lint_warnings"] = with_lint[-1].lint_warnings if with_lint else None
    feats["last_type_errors"] = with_types[-1].type_errors if with_types else None
    if len(with_lint) >= 2:
        last, before = with_lint[-1].lint_warnings, with_lint[-2].lint_warnings
        feats["lint_warnings_trend"] = (last or 0) - (before or 0)
    else:
        feats["lint_warnings_trend"] = None
    return feats
