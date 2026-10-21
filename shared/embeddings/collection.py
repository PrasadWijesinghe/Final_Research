"""Per-expert pgvector collections: `<expert_schema>.<name>` with vector(384) + tsvector."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from shared.embeddings.embedder import EMBEDDING_DIM, Embedder
from shared.embeddings.rrf import reciprocal_rank_fusion

# Only the four expert schemas may hold collections (never raw/platform/infra data tables).
EXPERT_SCHEMAS = frozenset({"dependency", "build", "testing", "infra"})
# DEFAULT: lowercase letters, digits and underscores, starting with a letter, max 48 chars.
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_WORD = re.compile(r"[A-Za-z0-9]{2,}")
# DEFAULT: each of the two searches fetches 50 candidates before fusion.
CANDIDATES = 50


class DimensionMismatchError(ValueError):
    """A vector's size does not match the table's vector(N) column."""


@dataclass(frozen=True)
class SearchHit:
    """One hybrid-search result."""

    doc_id: str
    content: str
    metadata: dict[str, Any]
    score: float


def quote_identifier(name: str) -> str:
    """Validate a SQL identifier (lowercase snake_case) and return it double-quoted."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return f'"{name}"'


def check_dimension(vector: Sequence[float], expected: int = EMBEDDING_DIM) -> None:
    """Raise DimensionMismatchError unless the vector has exactly `expected` values."""
    if len(vector) != expected:
        raise DimensionMismatchError(
            f"embedding has {len(vector)} dimensions but the table expects {expected}"
        )


def _literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _or_query(query: str) -> str:
    """Turn free text into a to_tsquery OR-expression (alphanumeric words only, so it is safe)."""
    words = list(dict.fromkeys(w.lower() for w in _WORD.findall(query)))
    return " | ".join(words)


class Collection:
    """A verified-case style collection inside one expert's schema."""

    def __init__(self, engine: Engine, embedder: Embedder, schema: str, name: str) -> None:
        if schema not in EXPERT_SCHEMAS:
            raise ValueError(
                f"schema {schema!r} is not an expert schema (allowed: {sorted(EXPERT_SCHEMAS)})"
            )
        self._engine = engine
        self._embedder = embedder
        self.schema = schema
        self.name = name
        self._table = f"{quote_identifier(schema)}.{quote_identifier(name)}"

    def ensure(self) -> None:
        """Create the table and indexes if missing; raise if it exists with another dimension."""
        dim = int(self._embedder.dim)
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {self._table} ("
                    "id bigserial PRIMARY KEY, doc_id text NOT NULL UNIQUE, "
                    "content text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}', "
                    f"embedding vector({dim}) NOT NULL, "
                    "tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED)"
                )
            )
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {quote_identifier(self.name + '_tsv_idx')} "
                    f"ON {self._table} USING gin (tsv)"
                )
            )
            # DEFAULT: HNSW with cosine distance (no training step, fine for small sets).
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {quote_identifier(self.name + '_vec_idx')} "
                    f"ON {self._table} USING hnsw (embedding vector_cosine_ops)"
                )
            )
        actual = self.table_dimension()
        if actual != dim:
            raise DimensionMismatchError(
                f"{self.schema}.{self.name} is vector({actual}) but the embedder makes {dim}"
            )

    def table_dimension(self) -> int:
        """Return N of the table's embedding vector(N) column."""
        with self._engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT a.atttypmod FROM pg_attribute a WHERE a.attrelid = "
                    "CAST(:rel AS regclass) AND a.attname = 'embedding' AND NOT a.attisdropped"
                ),
                {"rel": self._table},
            ).scalar_one_or_none()
        if value is None or value < 0:
            raise RuntimeError(f"{self.schema}.{self.name} has no vector(N) embedding column")
        return int(value)

    def add(self, doc_id: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Embed `content` and insert (or update) the document."""
        (vector,) = self._embedder.embed([content])
        check_dimension(vector, self.table_dimension())
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {self._table} (doc_id, content, metadata, embedding) "
                    "VALUES (:d, :c, CAST(:m AS jsonb), CAST(:e AS vector)) "
                    "ON CONFLICT (doc_id) DO UPDATE SET content = EXCLUDED.content, "
                    "metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding"
                ),
                {"d": doc_id, "c": content, "m": json.dumps(metadata or {}), "e": _literal(vector)},
            )

    def search(self, query: str, k: int = 5) -> list[SearchHit]:
        """Hybrid search: full-text and vector rankings merged by reciprocal rank fusion."""
        (vector,) = self._embedder.embed([query])
        check_dimension(vector, self.table_dimension())
        tsquery = _or_query(query)
        with self._engine.connect() as conn:
            vec_rows = conn.execute(
                text(
                    f"SELECT doc_id, content, metadata FROM {self._table} "
                    "ORDER BY embedding <=> CAST(:e AS vector), id LIMIT :n"
                ),
                {"e": _literal(vector), "n": CANDIDATES},
            ).all()
            fts_rows = (
                conn.execute(
                    text(
                        f"SELECT doc_id, content, metadata FROM {self._table} "
                        "WHERE tsv @@ to_tsquery('english', :q) "
                        "ORDER BY ts_rank(tsv, to_tsquery('english', :q)) DESC, id LIMIT :n"
                    ),
                    {"q": tsquery, "n": CANDIDATES},
                ).all()
                if tsquery
                else []
            )
        rows = {r[0]: r for r in [*vec_rows, *fts_rows]}
        fused = reciprocal_rank_fusion(
            [[r[0] for r in vec_rows], [r[0] for r in fts_rows]], limit=k
        )
        return [
            SearchHit(doc_id, rows[doc_id][1], dict(rows[doc_id][2]), score)
            for doc_id, score in fused
        ]
