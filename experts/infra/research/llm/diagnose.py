"""Provider-neutral interface for generating an infrastructure diagnosis."""

from __future__ import annotations

from typing import Protocol

from rag import RetrievedCase

from .prompts import build_diagnosis_prompt


class LLMClient(Protocol):
    """Minimum interface supplied later by the shared LLM gateway adapter."""

    def generate(self, prompt: str) -> str:
        """Return one model response for a prepared prompt."""
        ...


def diagnose(
    client: LLMClient,
    failure_summary: str,
    retrieved_cases: list[RetrievedCase],
) -> str:
    """Generate a diagnosis grounded in failure evidence and verified cases."""
    response = client.generate(build_diagnosis_prompt(failure_summary, retrieved_cases)).strip()
    if not response:
        raise ValueError("the LLM returned an empty diagnosis")
    return response
