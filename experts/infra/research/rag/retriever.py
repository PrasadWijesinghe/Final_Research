"""Small TF-IDF retriever for verified infrastructure failure cases."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer


@dataclass(frozen=True)
class RetrievedCase:
    """One similar verified case returned by retrieval."""

    case_id: str
    text: str
    score: float
    metadata: dict[str, Any]


class CaseRetriever:
    """Index and retrieve sanitised, human-verified failure cases."""

    def __init__(self) -> None:
        self.vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
        self.cases: list[dict[str, Any]] = []
        self.matrix: Any | None = None

    def fit(self, cases: Iterable[dict[str, Any]]) -> CaseRetriever:
        self.cases = [dict(case) for case in cases]
        if not self.cases:
            raise ValueError("at least one verified case is required")
        for case in self.cases:
            if not case.get("case_id") or not case.get("text"):
                raise ValueError("every case requires non-empty case_id and text fields")
        case_ids = [str(case["case_id"]) for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id values must be unique")
        self.matrix = self.vectorizer.fit_transform(case["text"] for case in self.cases)
        return self

    def retrieve(
        self, query: str, *, limit: int = 3, min_score: float = 0.0
    ) -> list[RetrievedCase]:
        if self.matrix is None:
            raise RuntimeError("fit or load the retriever before querying")
        if not query.strip():
            raise ValueError("query must not be empty")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if not 0 <= min_score <= 1:
            raise ValueError("min_score must be between 0 and 1")
        query_vector = self.vectorizer.transform([query])
        scores = (self.matrix @ query_vector.T).toarray().ravel()
        indexes = [int(index) for index in np.argsort(scores)[::-1] if scores[index] > min_score][
            : min(limit, len(self.cases))
        ]
        return [
            RetrievedCase(
                case_id=str(self.cases[index]["case_id"]),
                text=str(self.cases[index]["text"]),
                score=float(scores[index]),
                metadata={
                    key: value
                    for key, value in self.cases[index].items()
                    if key not in {"case_id", "text"}
                },
            )
            for index in indexes
        ]

    def save(self, path: Path) -> None:
        if self.matrix is None:
            raise RuntimeError("fit the retriever before saving")
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: Path) -> CaseRetriever:
        retriever = joblib.load(path)
        if not isinstance(retriever, cls):
            raise TypeError("artifact is not a CaseRetriever")
        return retriever
