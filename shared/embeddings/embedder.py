"""Embedders: the real granite model (lazy, optional extra) and a deterministic fake for tests."""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Sequence
from typing import Any, Protocol

EMBEDDING_DIM = 384
# DEFAULT: model name from the architecture doc; override with EMBEDDING_MODEL.
DEFAULT_MODEL = "ibm-granite/granite-embedding-small-english-r2"


class Embedder(Protocol):
    """Anything that turns texts into fixed-size vectors."""

    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text."""
        ...


class GraniteEmbedder:
    """granite-embedding-small-english-r2 via sentence-transformers, loaded on first use."""

    def __init__(self, model_name: str | None = None, cache_dir: str | None = None) -> None:
        self.model_name = model_name or os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL
        self.cache_dir = cache_dir or os.environ.get("EMBEDDING_CACHE_DIR") or None
        self.dim = EMBEDDING_DIM
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is not installed. Install the optional extra: "
                    "uv sync --all-packages --extra embeddings (pipelineiq-shared[embeddings])."
                ) from exc
            self._model = SentenceTransformer(self.model_name, cache_folder=self.cache_dir)
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one normalised 384-d vector per text."""
        if not texts:
            return []
        raw = self._load().encode(list(texts), normalize_embeddings=True)
        vectors = [[float(x) for x in row] for row in raw]
        for vector in vectors:
            if len(vector) != self.dim:
                raise ValueError(f"model returned {len(vector)} dimensions, expected {self.dim}")
        return vectors


_TOKEN = re.compile(r"[a-z0-9_]+")


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder for tests: no model, no network.

    # DEFAULT: hashed word counts (so texts sharing words are close) instead of random vectors.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one unit-length vector per text."""
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else vector


def get_embedder() -> Embedder:
    """Return the granite embedder (EMBEDDING_PROVIDER=fake gives the fake one)."""
    if os.environ.get("EMBEDDING_PROVIDER", "granite").strip().lower() == "fake":
        return FakeEmbedder()
    return GraniteEmbedder()
