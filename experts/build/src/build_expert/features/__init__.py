"""Pre-run features for the Build expert, shared by dataset building, training and serving.

Four groups (docs/experts/build.md section 4): A what the code change looks like, B build-tool
configuration changes, C the CI environment, D history of earlier commits (no look-ahead).
"""

from __future__ import annotations

from datetime import datetime

from shared.contracts import CommitContext

from . import change, config, environment, history
from .history import CommitOutcome, HistoryIndex
from .patches import module_dirs

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "change": change.NAMES,
    "config": config.NAMES,
    "environment": environment.NAMES,
    "history": history.NAMES,
}
FEATURE_NAMES: tuple[str, ...] = tuple(n for names in FEATURE_GROUPS.values() for n in names)
# Text-valued features; everything else is numeric or boolean.
CATEGORICAL_FEATURES: tuple[str, ...] = ("package_manager",)

__all__ = [
    "CATEGORICAL_FEATURES",
    "FEATURE_GROUPS",
    "FEATURE_NAMES",
    "CommitOutcome",
    "HistoryIndex",
    "commit_features",
    "full_features",
    "history_for",
    "module_dirs",
]


def commit_features(ctx: CommitContext, *, default_branch: str | None) -> dict[str, object]:
    """Groups A, B and C: everything that comes from the commit context alone."""
    feats: dict[str, object] = {}
    feats.update(change.change_features(ctx))
    feats.update(config.config_change_features(ctx))
    feats.update(environment.environment_features(ctx, default_branch=default_branch))
    return feats


def history_for(
    index: HistoryIndex,
    *,
    repo: str,
    cutoff: datetime,
    branch: str,
    author_hash: str | None,
    dirs: frozenset[str],
    default_branch: str | None,
) -> dict[str, object]:
    """Group D: history before `cutoff` (see features/history.py)."""
    return history.history_features(
        index,
        repo=repo,
        cutoff=cutoff,
        branch=branch,
        author_hash=author_hash,
        dirs=dirs,
        default_branch=default_branch,
    )


def full_features(
    ctx: CommitContext, index: HistoryIndex, *, default_branch: str | None
) -> dict[str, object]:
    """All four groups for one commit, exactly as the dataset builder computes them; history
    uses only outcomes finished before `ctx.history_cutoff`. Used by /predict."""
    feats = commit_features(ctx, default_branch=default_branch)
    hist = history_for(
        index,
        repo=ctx.repo.full_name,
        cutoff=ctx.history_cutoff,
        branch=ctx.branch,
        author_hash=ctx.author_hash,
        dirs=module_dirs(ctx.changed_files),
        default_branch=default_branch,
    )
    for name, value in hist.items():
        feats.setdefault(name, value)
    return feats
