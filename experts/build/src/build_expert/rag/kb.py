"""The Build knowledge base (PLAN B10): past build failures and the commits that fixed them.

Cases come from a dataset's `fix_candidates.parquet` (a build failure followed on the same
branch within 7 days by a green commit). They are stored in the shared hybrid-search table
`build.kb_cases` (`shared.embeddings.Collection`: keyword and vector search merged by RRF).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from shared.contracts import RetrievedCase
from shared.embeddings import Collection, normalise_log
from shared.embeddings.embedder import Embedder

logger = logging.getLogger(__name__)

SCHEMA, NAME = "build", "kb_cases"
# DEFAULT: the best RRF score a hit can get is 2/61 (rank 1 in both lists, k = 60); similarity is
# the score as a share of that. Hits below MIN_SIMILARITY (not near the top of both lists) are
# dropped, so an unrelated case is never cited just because the table is small.
BEST_RRF = 2 / 61
MIN_SIMILARITY = 0.5
# DEFAULT: characters of the fix message and error text kept per case.
MAX_FIX_MESSAGE = 300
MAX_CASE_ERRORS = 8


class CaseSearch(Protocol):
    """What the diagnosis needs from a knowledge base."""

    def search(self, query: str, k: int, kind: str | None = None) -> list[RetrievedCase]: ...


def case_text(sub_labels: str, error_codes: str, locations: list[dict[str, Any]]) -> str:
    """The searchable text of a failure: kinds, error codes and normalised messages."""
    # No fixed labels such as "kind:" in the text: a word every case shares would make every case
    # a keyword match.
    parts = [(sub_labels or "build").replace(",", " "), (error_codes or "").replace(",", " ")]
    for loc in locations[:MAX_CASE_ERRORS]:
        msg = normalise_log(str(loc.get("message") or ""))
        parts.append(" ".join(x for x in (loc.get("tool"), loc.get("code"), msg) if x))
    return "\n".join(parts)


@dataclass(frozen=True)
class KbCase:
    """One fix candidate ready to store."""

    doc_id: str
    content: str
    metadata: dict[str, Any]


def cases_from_fix_candidates(
    rows: Iterable[dict[str, Any]],
    *,
    excluded_repos: frozenset[str] = frozenset(),
    strong_only: bool = True,
) -> list[KbCase]:
    """Turn fix_candidates.parquet rows into cases.

    `strong_only` keeps a case only when the fix touched a file named in the errors
    (`overlap_paths`), which makes it likely that the fix commit really fixed this failure.
    """
    out: list[KbCase] = []
    for r in rows:
        if r["repo"] in excluded_repos:
            continue
        overlap = json.loads(r.get("overlap_paths") or "[]")
        if strong_only and not overlap:
            continue
        locations = json.loads(r.get("error_locations") or "[]")
        repo, fix_sha = r["repo"], r["fix_sha"]
        out.append(
            KbCase(
                doc_id=f"{repo}@{r['failed_sha'][:12]}..{fix_sha[:12]}",
                content=case_text(r.get("sub_labels") or "", r.get("error_codes") or "", locations),
                metadata={
                    "repo": repo,
                    "failed_sha": r["failed_sha"],
                    "fix_sha": fix_sha,
                    "sub_labels": r.get("sub_labels"),
                    "error_codes": r.get("error_codes"),
                    "fix_message": (r.get("fix_message") or "")[:MAX_FIX_MESSAGE],
                    "fix_changed_paths": json.loads(r.get("fix_changed_paths") or "[]")[:20],
                    "overlap_paths": overlap[:20],
                    "hours_to_fix": r.get("hours_to_fix"),
                    "licence": r.get("licence"),
                    "first_error": (locations[0].get("message") if locations else None),
                    "source_url": f"https://github.com/{repo}/commit/{fix_sha}",
                    # PLAN B10: mined automatically; a person still has to review each case.
                    "reviewed": False,
                },
            )
        )
    return out


class KnowledgeBase:
    """`build.kb_cases` through the shared Collection."""

    def __init__(self, engine: Engine, embedder: Embedder) -> None:
        self._engine = engine
        self.collection = Collection(engine, embedder, SCHEMA, NAME)

    def load(self, cases: Iterable[KbCase]) -> int:
        """Create the table if needed and add (or update) every case."""
        self.collection.ensure()
        n = 0
        for c in cases:
            self.collection.add(c.doc_id, c.content, c.metadata)
            n += 1
        return n

    def count(self) -> int:
        """Stored cases (0 when the table does not exist yet)."""
        try:
            with self._engine.connect() as conn:
                return int(conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.{NAME}")).scalar_one())
        except SQLAlchemyError:
            return 0

    def search(self, query: str, k: int, kind: str | None = None) -> list[RetrievedCase]:
        """Similar past failures as contract cases, best first.

        With `kind` (tsc, eslint, format, bundler, build_other), only cases of that kind count:
        a fixed lint error says little about a type error.
        """
        out: list[RetrievedCase] = []
        for hit in self.collection.search(query, k=k * 4 if kind else k):
            similarity = min(1.0, hit.score / BEST_RRF)
            m = hit.metadata
            if similarity < MIN_SIMILARITY or len(out) == k:
                continue
            if kind and kind not in str(m.get("sub_labels") or "").split(","):
                continue
            first = m.get("first_error") or ""
            out.append(
                RetrievedCase(
                    case_id=hit.doc_id,
                    similarity=round(similarity, 3),
                    summary=(
                        f"{m.get('repo')}: {m.get('sub_labels') or 'build'} failure "
                        f"({m.get('error_codes') or 'no codes'}) {first}"
                    )[:300],
                    fix_summary=(
                        f"Fixed by: {m.get('fix_message') or '(no message)'}; changed "
                        f"{', '.join(m.get('overlap_paths') or m.get('fix_changed_paths') or [])}"
                    )[:400],
                    source_url=m.get("source_url"),
                )
            )
        return out


def open_kb(engine: Engine | None, embedder: Embedder) -> KnowledgeBase | None:
    """A knowledge base when the database is reachable, else None (diagnosis runs without)."""
    if engine is None:
        return None
    try:
        kb = KnowledgeBase(engine, embedder)
        kb.collection.table_dimension()
        return kb
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("knowledge base not available", extra={"error": str(exc)})
        return None


_DEFAULT: list[KnowledgeBase | None] = []


def default_kb() -> KnowledgeBase | None:
    """The knowledge base from DATABASE_URL and EMBEDDING_PROVIDER, opened once per process."""
    if not _DEFAULT:
        from shared.db.session import get_engine
        from shared.embeddings import get_embedder

        try:
            engine: Engine | None = get_engine()
        except RuntimeError:  # DATABASE_URL not set
            engine = None
        _DEFAULT.append(open_kb(engine, get_embedder()))
    return _DEFAULT[0]
