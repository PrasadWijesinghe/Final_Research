"""Retrieval sanity check for the knowledge base (P4.10). Small and honest, not a benchmark.

For a sample of mined genuine cases, ask the knowledge base the question a live failure would ask
(the failure text only, as of the failure's own time) and see whether the top 5 hold something
relevant. The case itself and everything not yet known at its failure time are excluded by the
same filters the live path uses. "Relevant" is judged four ways, because no single one is right:

| relevance | meaning |
|---|---|
| same_repo | a retrieved case comes from the same repository (easy: repos have habits) |
| same_family | ... has the same primary error family (the query text already says it) |
| same_test | ... failed in a test with the same full name (the strongest) |
| same_fix | ... shares a fix tag that says something (not misc, test_only, source_change, dependency_update) |

For each, `hit@5` is "at least one of the top 5 is relevant" and `p@5` the share that are. A random
baseline is computed from the eligible pool of each query (what picking 5 cases blindly gives).
"""
# ruff: noqa: E501

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from shared.embeddings import Embedder

from .retrieve import Hit, Mode, Query, query_embedder, search
from .settings import KbSettings, load_settings, table

# DEFAULT: tags that describe the scope of a change, not what kind of fix it was.
UNINFORMATIVE_FIX = frozenset(
    {"fix:misc", "fix:test_only", "fix:source_change", "fix:dependency_update"}
)
MODES: tuple[Mode, ...] = ("fts", "vector", "hybrid")
RELEVANCES = ("same_repo", "same_family", "same_test", "same_fix")


@dataclass(frozen=True)
class CaseInfo:
    """The columns of a case the evaluation needs."""

    case_id: str
    kind: str
    repo: str
    family: str
    tests: frozenset[str]
    fixes: frozenset[str]
    failed_at: datetime
    verified_at: datetime
    run_id: int
    sha: str
    text: str  # the failure part of the indexed text: what a live query would contain


def load_cases(conn: psycopg.Connection[Any]) -> list[CaseInfo]:
    """Every embedded case."""
    with conn.cursor() as cur:
        cur.execute(
            "select case_id, kind, repo, error_family, failing_tests, tags, failed_at, verified_at, "
            f"failed_run_id, failed_sha, search_text from {table()} where embedding is not null"
        )
        rows = cur.fetchall()
    return [
        CaseInfo(
            c,
            k,
            r,
            f,
            frozenset(t or ()),
            frozenset(
                x for x in (tags or ()) if x.startswith("fix:") and x not in UNINFORMATIVE_FIX
            ),
            fa,
            va,
            run,
            sha,
            text.split("\nroot cause:")[0],
        )  # fmt: skip
        for c, k, r, f, t, tags, fa, va, run, sha, text in rows
    ]


def relevant(query: CaseInfo, other: CaseInfo, kind: str) -> bool:
    """Is `other` relevant to `query` under the relevance `kind`?"""
    if kind == "same_repo":
        return other.repo == query.repo
    if kind == "same_family":
        return other.family == query.family
    if kind == "same_test":
        return bool(query.tests & other.tests)
    return bool(query.fixes & other.fixes)


def eligible(query: CaseInfo, pool: Sequence[CaseInfo]) -> list[CaseInfo]:
    """The cases the live filters allow for this query."""
    return [
        o for o in pool
        if o.verified_at <= query.failed_at and o.run_id != query.run_id and o.sha != query.sha
    ]  # fmt: skip


def random_hit_rate(n_pool: int, n_relevant: int, k: int = 5) -> float:
    """P(at least one of k blind picks, without replacement, is relevant)."""
    if n_relevant == 0 or n_pool == 0:
        return 0.0
    k = min(k, n_pool)
    return 1.0 - math.comb(n_pool - n_relevant, k) / math.comb(n_pool, k)


def evaluate(
    conn: psycopg.Connection[Any],
    sample: int = 300,
    seed: int = 0,
    k: int = 5,
    embedder: Embedder | None = None,
    cfg: KbSettings | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Run the three retrieval modes on a seeded sample of genuine cases; return the numbers."""
    cfg = cfg or load_settings()
    emb = embedder or query_embedder()
    cases = load_cases(conn)
    genuine = [c for c in cases if c.kind == "genuine"]
    rng = random.Random(seed)
    queries = []
    for c in sorted(genuine, key=lambda c: c.case_id):
        if len(eligible(c, cases)) >= k:
            queries.append(c)
    rng.shuffle(queries)
    queries = queries[:sample]
    by_id = {c.case_id: c for c in cases}
    agg: dict[str, dict[str, dict[str, float]]] = {
        m: {r: {"hit": 0.0, "p": 0.0, "n": 0.0} for r in RELEVANCES} for m in (*MODES, "random")
    }
    for i, q in enumerate(queries, start=1):
        pool = eligible(q, cases)
        hits_by_mode: dict[str, list[Hit]] = {
            m: search(
                conn,
                Query(q.text, q.failed_at, q.run_id, q.sha),
                k=k,
                mode=m,
                embedder=emb,
                cfg=cfg,
            )
            for m in MODES
        }
        for rel in RELEVANCES:
            n_rel = sum(relevant(q, o, rel) for o in pool)
            if rel == "same_fix" and not q.fixes:
                continue  # nothing to match: the query case has no informative fix tag
            if n_rel == 0:
                continue  # no eligible case could be relevant: skip rather than count a miss
            agg["random"][rel]["hit"] += random_hit_rate(len(pool), n_rel, k)
            agg["random"][rel]["p"] += n_rel / len(pool)
            agg["random"][rel]["n"] += 1
            for m in MODES:
                got = [relevant(q, by_id[h.case_id], rel) for h in hits_by_mode[m]]
                agg[m][rel]["hit"] += float(any(got))
                agg[m][rel]["p"] += sum(got) / max(len(got), 1)
                agg[m][rel]["n"] += 1
        if progress:
            progress(i, len(queries))
    table = {
        m: {
            r: {
                "n": int(v["n"]),
                "hit@5": round(v["hit"] / v["n"], 3) if v["n"] else None,
                "p@5": round(v["p"] / v["n"], 3) if v["n"] else None,
            }
            for r, v in rels.items()
        }
        for m, rels in agg.items()
    }
    return {
        "n_queries": len(queries),
        "n_cases": len(cases),
        "sample": sample,
        "seed": seed,
        "k": k,
        "table": table,
        "distribution": distribution(conn),
    }


def distribution(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """Counts by repo, kind, licence, tag and mining rule."""
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        cur.execute(f"select repo, kind, count(*) from {table()} group by 1, 2 order by 1, 2")
        out["by_repo_kind"] = [list(r) for r in cur.fetchall()]
        cur.execute(f"select licence, count(*) from {table()} group by 1 order by 2 desc")
        out["by_licence"] = [list(r) for r in cur.fetchall()]
        cur.execute(f"select kind, mine_rule, count(*) from {table()} group by 1, 2 order by 1, 2")
        out["by_rule"] = [list(r) for r in cur.fetchall()]
        cur.execute(
            f"select kind, t, count(*) from {table()}, unnest(tags) t group by 1, 2 "
            "order by 3 desc, 2"
        )
        tags: dict[str, Counter[str]] = {}
        for kind, tag, n in cur.fetchall():
            tags.setdefault(kind, Counter())[tag] = n
        out["tags"] = {k: dict(v) for k, v in tags.items()}
        cur.execute(
            f"select count(*), count(embedding), min(verified_at), max(verified_at) from {table()}"
        )
        n, e, lo, hi = cur.fetchone()  # type: ignore[misc]
        out["totals"] = {"cases": n, "embedded": e, "first": str(lo), "last": str(hi)}
    return out
