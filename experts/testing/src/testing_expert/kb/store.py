"""Write cases into `testing.kb_cases` and embed them (P4.10). Idempotent and resumable.

`upsert_cases` is keyed on the deterministic `case_id`; a case whose indexed text is unchanged keeps
its embedding, one whose text changed loses it and is embedded again by `embed_pending`.
`embed_pending` embeds only rows with no embedding, in batches, and commits after every batch, so
a stopped run continues where it stopped. The model is NEVER replaced by a fake one here: when it
cannot load, the error is raised (set `allow_fake=True` only for tests).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from shared.embeddings import Embedder, get_embedder
from shared.embeddings.embedder import DEFAULT_MODEL

from .mine import CaseRow
from .settings import KbSettings, load_settings, table
from .text import plain, vector_literal

log = logging.getLogger(__name__)

_UPSERT = """
insert into {t} as k (
  case_id, payload, tags, kind, repo, licence, failed_job_id, failed_run_id, failed_sha, failed_at,
  verified_at, fix_sha, error_family, failing_tests, search_text, text_hash, diff_excerpt, links,
  mine_rule, search)
values (%(case_id)s, %(payload)s::jsonb, %(tags)s, %(kind)s, %(repo)s, %(licence)s, %(job)s,
        %(run)s, %(sha)s, %(failed_at)s, %(verified_at)s, %(fix_sha)s, %(family)s, %(tests)s,
        %(text)s, %(hash)s, %(excerpt)s, %(links)s::jsonb, %(rule)s,
        to_tsvector('english', %(plain)s))
on conflict (case_id) do update set
  payload = excluded.payload, tags = excluded.tags, kind = excluded.kind, repo = excluded.repo,
  licence = excluded.licence, failed_job_id = excluded.failed_job_id,
  failed_run_id = excluded.failed_run_id, failed_sha = excluded.failed_sha,
  failed_at = excluded.failed_at, verified_at = excluded.verified_at, fix_sha = excluded.fix_sha,
  error_family = excluded.error_family, failing_tests = excluded.failing_tests,
  diff_excerpt = excluded.diff_excerpt, links = excluded.links, mine_rule = excluded.mine_rule,
  embedding = case when k.text_hash is not distinct from excluded.text_hash
                   then k.embedding end,
  embedding_model = case when k.text_hash is not distinct from excluded.text_hash
                         then k.embedding_model end,
  embedded_at = case when k.text_hash is not distinct from excluded.text_hash
                     then k.embedded_at end,
  search_text = excluded.search_text, text_hash = excluded.text_hash, search = excluded.search"""


def upsert_cases(conn: psycopg.Connection[Any], rows: Sequence[CaseRow]) -> int:
    """Insert or update the cases (embeddings of unchanged text are kept). Returns the count."""
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                _UPSERT.format(t=table()),
                {
                    "case_id": r.case.case_id,
                    "payload": r.case.model_dump_json(),
                    "tags": r.case.tags,
                    "kind": r.kind,
                    "repo": r.repo,
                    "licence": r.licence,
                    "job": r.failed_job_id,
                    "run": r.failed_run_id,
                    "sha": r.failed_sha,
                    "failed_at": r.failed_at,
                    "verified_at": r.verified_at,
                    "fix_sha": r.fix_sha,
                    "family": r.error_family,
                    "tests": list(r.failing_tests),
                    "text": r.search_text,
                    "hash": r.text_hash,
                    "excerpt": r.diff_excerpt,
                    "links": json.dumps(r.links),
                    "rule": r.mine_rule,
                    "plain": plain(r.search_text),
                },
            )
    conn.commit()
    return len(rows)


def model_name() -> str:
    """The embedding model in use (EMBEDDING_MODEL or the default granite small)."""
    return os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL


def pending_count(conn: psycopg.Connection[Any]) -> int:
    """Cases that still need an embedding."""
    with conn.cursor() as cur:
        cur.execute(f"select count(*) from {table()} where embedding is null")
        return int(cur.fetchone()[0])  # type: ignore[index]


def embed_pending(
    conn: psycopg.Connection[Any],
    embedder: Embedder | None = None,
    cfg: KbSettings | None = None,
    allow_fake: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, float]:
    """Embed every case without a vector, in committed batches. Returns counts and timings."""
    cfg = cfg or load_settings()
    todo = pending_count(conn)
    if todo == 0:
        return {"embedded": 0, "seconds": 0.0}
    if embedder is None:
        if os.environ.get("EMBEDDING_PROVIDER", "granite").strip().lower() == "fake":
            if not allow_fake:
                raise RuntimeError(
                    "EMBEDDING_PROVIDER=fake would fill the knowledge base with a stand-in model; "
                    "unset it, or pass allow_fake=True for a test."
                )
        embedder = get_embedder()
    name = "fake" if type(embedder).__name__ == "FakeEmbedder" else model_name()
    started = time.perf_counter()
    done = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"select case_id, search_text from {table()} where embedding is null "
                "order by case_id limit %s",
                (cfg.embed_batch_size,),
            )
            batch = cur.fetchall()
        if not batch:
            break
        vectors = embedder.embed([t[: cfg.embed_max_chars] for _, t in batch])
        with conn.cursor() as cur:
            for (case_id, _), vec in zip(batch, vectors, strict=True):
                cur.execute(
                    f"update {table()} set embedding = %s::vector, embedding_model = %s, "
                    "embedded_at = now() where case_id = %s",
                    (vector_literal(vec), name, case_id),
                )
        conn.commit()
        done += len(batch)
        log.info("embedded", extra={"done": done, "total": todo})
        if progress:
            progress(done, todo)
    return {"embedded": done, "seconds": round(time.perf_counter() - started, 1)}
