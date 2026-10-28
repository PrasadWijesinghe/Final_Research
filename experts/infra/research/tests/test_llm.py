from __future__ import annotations

from llm import diagnose
from rag import RetrievedCase


class FakeClient:
    def generate(self, prompt: str) -> str:
        assert "Failure evidence" in prompt
        assert "case-1" in prompt
        return "The deployment token did not have the required permission."


def test_diagnose_uses_evidence_and_retrieved_cases() -> None:
    cases = [
        RetrievedCase(
            case_id="case-1",
            text="A deployment failed after permission was denied.",
            score=0.8,
            metadata={},
        )
    ]

    result = diagnose(FakeClient(), "The deployment step returned HTTP 403.", cases)

    assert "permission" in result
