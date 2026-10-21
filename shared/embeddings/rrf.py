"""Reciprocal rank fusion: merge several ranked lists of ids into one."""

from __future__ import annotations

from collections.abc import Hashable, Sequence

# DEFAULT: k=60, the value from the original RRF paper and the usual choice.
RRF_K = 60


def reciprocal_rank_fusion[T: Hashable](
    rankings: Sequence[Sequence[T]], k: int = RRF_K, limit: int | None = None
) -> list[tuple[T, float]]:
    """Fuse ranked id lists (best first); return (id, score) sorted by score, best first."""
    scores: dict[T, float] = {}
    first_seen: dict[T, int] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(item, len(first_seen))
    # Ties break by first appearance so the output is deterministic.
    fused = sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return fused if limit is None else fused[:limit]
