"""Embeddings, log normalisation, per-expert pgvector collections and hybrid search."""

from shared.embeddings.collection import (
    EXPERT_SCHEMAS,
    Collection,
    DimensionMismatchError,
    SearchHit,
    check_dimension,
    quote_identifier,
)
from shared.embeddings.embedder import (
    EMBEDDING_DIM,
    Embedder,
    FakeEmbedder,
    GraniteEmbedder,
    get_embedder,
)
from shared.embeddings.normalise import normalise_log
from shared.embeddings.rrf import reciprocal_rank_fusion

__all__ = [
    "EMBEDDING_DIM",
    "EXPERT_SCHEMAS",
    "Collection",
    "DimensionMismatchError",
    "Embedder",
    "FakeEmbedder",
    "GraniteEmbedder",
    "SearchHit",
    "check_dimension",
    "get_embedder",
    "normalise_log",
    "quote_identifier",
    "reciprocal_rank_fusion",
]
