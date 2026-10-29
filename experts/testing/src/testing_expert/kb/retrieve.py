"""Hybrid retrieval over the knowledge base (P4.10): full-text + vector, merged by RRF.

`search` runs two rankings over `testing.kb_cases`, each keeping its best `candidates` rows:

1. full text: `ts_rank` of the English `search` tsvector against an OR of the query's words;
2. vector: pgvector cosine distance between the query embedding and the case embedding.

They are merged with reciprocal rank fusion (shared `reciprocal_rank_fusion`, k = 60) and the top
`top_k` (5) are returned. Both rankings only see rows that pass these filters, in SQL:

* **no future leakage:** `verified_at <= cutoff`. A case is known only once its fix (or its re-run)
  passed, so a failure at time T cannot be helped by a fix that came after T.
* **no self-retrieval:** cases from the query's own run or commit are left out.
* **licence hook:** with `permissive_only=True`, only cases from permissive licences (config list)
  can come back. This is the switch for a commercial knowledge base (docs/DATA.md section 6).

Anything that can go wrong degrades to "no cases" in `retrieve_for_failure`: no database, an empty
table, no embedding model (then only the full-text ranking runs).
"""
# ruff: noqa: E501

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal

import psycopg

from shared.contracts import RoutedFailure
from shared.embeddings import Embedder, get_embedder, reciprocal_rank_fusion
from testing_expert.triage.features import LogEvidence

from .settings import KbSettings, load_settings, table
from .text import failure_part, plain, vector_literal

log = logging.getLogger(__name__)

Mode = Literal["hybrid", "fts", "vector"]
# DEFAULT: after the embedding model fails to load in a process, the vector side is skipped for 5
# minutes (full-text only), so a missing torch or a blocked download does not slow every diagnosis.
VECTOR_RETRY_SECONDS = 300.0
_vector_off_until = 0.0
_WORD = re.compile(r"[A-Za-z0-9]{2,}")
# DEFAULT: words that every case text contains because of its fixed layout carry no signal.
_TEMPLATE_WORDS = frozenset({"error", "family", "test", "file"})


@dataclass(frozen=True)
class Query:
    """What to look for, and which cases must stay out."""

    text: str
    cutoff: datetime | None = None  # only cases verified at or before this time
    run_id: int | None = None  # leave out cases that failed in this run
    head_sha: str | None = None  # ... or on this commit


@dataclass(frozen=True)
class Hit:
    """One retrieved case."""

    case_id: str
    score: float  # fused RRF score
    similarity: float  # score / best possible score, 0..1
    kind: str
    repo: str
    failure_summary: str
    fix_summary: str
    source_url: str | None
    tags: tuple[str, ...]
    verified_at: datetime | None
    fts_rank: int | None
    vector_rank: int | None
    case: dict[str, Any]


def or_query(text: str, max_words: int) -> str:
    """`a | b | c` from the distinct alphanumeric words of the text (safe for to_tsquery)."""
    words = dict.fromkeys(
        w.lower() for w in _WORD.findall(plain(text)) if w.lower() not in _TEMPLATE_WORDS
    )
    return " | ".join(list(words)[:max_words])


def _filters(q: Query, permissive: Sequence[str] | None) -> tuple[str, dict[str, Any]]:
    where = ["true"]
    params: dict[str, Any] = {}
    if q.cutoff is not None:
        where.append("verified_at <= %(cutoff)s")
        params["cutoff"] = q.cutoff
    if q.run_id is not None:
        where.append("failed_run_id is distinct from %(run)s")
        params["run"] = q.run_id
    if q.head_sha is not None:
        where.append("failed_sha is distinct from %(sha)s")
        params["sha"] = q.head_sha
    if permissive is not None:
        where.append("licence = any(%(permissive)s)")
        params["permissive"] = list(permissive)
    return " and ".join(where), params


@lru_cache(maxsize=1)
def query_embedder() -> Embedder:
    """The embedder used for queries, created once (the granite model loads on first use)."""
    return get_embedder()


def search(
    conn: psycopg.Connection[Any],
    q: Query,
    *,
    k: int | None = None,
    mode: Mode = "hybrid",
    embedder: Embedder | None = None,
    permissive_only: bool = False,
    cfg: KbSettings | None = None,
) -> list[Hit]:
    """Top-k cases for the query. `mode` picks one ranking or both (the default, fused)."""
    cfg = cfg or load_settings()
    k = k or cfg.top_k
    text = q.text[: cfg.max_query_chars]
    where, params = _filters(q, cfg.permissive_licences if permissive_only else None)
    rankings: list[list[str]] = []
    ranks: dict[str, dict[str, int]] = {}

    def add(name: str, ids: list[str]) -> None:
        rankings.append(ids)
        for i, case_id in enumerate(ids, start=1):
            ranks.setdefault(case_id, {})[name] = i

    if mode in ("hybrid", "fts"):
        tsq = or_query(text, cfg.max_query_words)
        if tsq:
            with conn.cursor() as cur:
                cur.execute(
                    f"select case_id from {table()} where {where} "
                    "and search @@ to_tsquery('english', %(q)s) "
                    "order by ts_rank(search, to_tsquery('english', %(q)s)) desc, case_id limit %(n)s",
                    {**params, "q": tsq, "n": cfg.candidates},
                )
                add("fts", [r[0] for r in cur.fetchall()])
    if mode in ("hybrid", "vector"):
        emb = embedder or query_embedder()
        (vec,) = emb.embed([text])
        with conn.cursor() as cur:
            # DEFAULT: iterative scan, so the filters above cannot leave the HNSW search short
            # of candidates (pgvector 0.8+; ignored by an older server).
            try:
                cur.execute("set local hnsw.iterative_scan = relaxed_order")
            except psycopg.Error:
                conn.rollback()
            cur.execute(
                f"select case_id from {table()} where {where} and embedding is not null "
                "order by embedding <=> %(v)s::vector, case_id limit %(n)s",
                {**params, "v": vector_literal(vec), "n": cfg.candidates},
            )
            add("vector", [r[0] for r in cur.fetchall()])
    if not rankings:
        return []
    fused = reciprocal_rank_fusion(rankings, k=cfg.rrf_k, limit=k)
    best = len(rankings) / (cfg.rrf_k + 1)
    ids = [case_id for case_id, _ in fused]
    with conn.cursor() as cur:
        cur.execute(
            "select case_id, kind, repo, payload, links, tags, verified_at "
            f"from {table()} where case_id = any(%s)",
            (ids,),
        )
        rows = {r[0]: r for r in cur.fetchall()}
    conn.rollback()  # read-only: end the transaction so `set local` does not linger
    hits = []
    for case_id, score in fused:
        _, kind, repo, payload, links, tags, verified = rows[case_id]
        link = (links or {}).get("fix_commit" if kind == "genuine" else "verifying_run")
        hits.append(
            Hit(
                case_id=case_id,
                score=score,
                similarity=max(0.0, min(1.0, score / best)),
                kind=kind,
                repo=repo,
                failure_summary=payload["failure_summary"],
                fix_summary=payload["fix_summary"],
                source_url=link,
                tags=tuple(tags or ()),
                verified_at=verified,
                fts_rank=ranks[case_id].get("fts"),
                vector_rank=ranks[case_id].get("vector"),
                case=payload,
            )
        )
    return hits


# --- the diagnose path --------------------------------------------------------------------------


def _connect() -> psycopg.Connection[Any] | None:
    url = os.environ.get("DATABASE_URL")
    return psycopg.connect(url.replace("+psycopg", ""), connect_timeout=3) if url else None


_COUNT_TTL = 30.0  # DEFAULT: /health re-reads the case count at most every 30 seconds
_count_cache: tuple[float, int] = (-1e9, 0)


def count_cases() -> int:
    """Number of cases in the knowledge base; 0 when there is no database or no table."""
    global _count_cache
    now = time.monotonic()
    if now - _count_cache[0] < _COUNT_TTL:
        return _count_cache[1]
    n = 0
    try:
        url = os.environ.get("DATABASE_URL")
        if url:
            with psycopg.connect(url.replace("+psycopg", ""), connect_timeout=1) as conn:
                n = int(conn.execute(f"select count(*) from {table()}").fetchone()[0])  # type: ignore[index]
    except Exception:
        log.debug("knowledge-base count unavailable", exc_info=True)
    _count_cache = (now, n)
    return n


def failure_cutoff(conn: psycopg.Connection[Any], failure: RoutedFailure) -> datetime:
    """When the failed job finished (from `raw.jobs`), else when the router saw it."""
    try:
        with conn.cursor() as cur:
            cur.execute("select completed_at from raw.jobs where job_id = %s", (failure.job_id,))
            row = cur.fetchone()
        if row and row[0] is not None:
            return row[0]  # type: ignore[no-any-return]
    except psycopg.Error:
        conn.rollback()
        log.warning("job time lookup failed, using routed_at", exc_info=True)
    return failure.routed_at


def retrieve_for_failure(
    failure: RoutedFailure,
    log_evidence: LogEvidence,
    *,
    permissive_only: bool = False,
    cfg: KbSettings | None = None,
) -> list[Hit]:
    """Top cases for a live failure; an empty list when the knowledge base cannot answer."""
    cfg = cfg or load_settings()
    conn = None
    try:
        conn = _connect()
        if conn is None:
            return []
        with conn.cursor() as cur:
            cur.execute(f"select exists (select 1 from {table()} limit 1)")
            if not cur.fetchone()[0]:  # type: ignore[index]
                return []
        conn.rollback()
        text, _ = failure_part(
            log_evidence.failures, failure.log_excerpt[-4000:], cfg.max_signature_tests
        )
        q = Query(
            text=text,
            cutoff=failure_cutoff(conn, failure),
            run_id=failure.run_id,
            head_sha=failure.head_sha,
        )
        global _vector_off_until
        if time.monotonic() >= _vector_off_until:
            try:
                return search(conn, q, permissive_only=permissive_only, cfg=cfg)
            except Exception:
                # Most likely the embedding model is missing or cannot load: use the words only.
                _vector_off_until = time.monotonic() + VECTOR_RETRY_SECONDS
                log.warning("vector search unavailable, using full text only", exc_info=True)
                conn.rollback()
        return search(conn, q, mode="fts", permissive_only=permissive_only, cfg=cfg)
    except Exception:
        log.warning("knowledge base unavailable, no cases retrieved", exc_info=True)
        return []
    finally:
        if conn is not None:
            conn.close()
