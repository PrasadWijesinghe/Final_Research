from __future__ import annotations

from pathlib import Path

from rag import CaseRetriever


def test_retriever_returns_and_reloads_the_closest_case(tmp_path: Path) -> None:
    cases = [
        {"case_id": "runner", "text": "self hosted runner was offline"},
        {"case_id": "permission", "text": "deployment permission was denied"},
    ]
    retriever = CaseRetriever().fit(cases)

    result = retriever.retrieve("runner offline", limit=1)
    path = tmp_path / "retriever.joblib"
    retriever.save(path)
    reloaded = CaseRetriever.load(path)

    assert result[0].case_id == "runner"
    assert reloaded.retrieve("permission denied", limit=1)[0].case_id == "permission"


def test_retriever_does_not_return_zero_similarity_cases() -> None:
    retriever = CaseRetriever().fit(
        [{"case_id": "runner", "text": "self hosted runner was offline"}]
    )

    assert retriever.retrieve("database migration schema") == []
