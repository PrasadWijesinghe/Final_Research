"""Diagnosis on real tool output (log fixtures), with a fake knowledge base and a mocked gateway."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from build_expert.diagnose import diagnose
from build_expert.rag import llm

from shared.contracts import BuildDetails, FailureCategory, RetrievedCase, RoutedFailure

FIXTURES = Path(__file__).parent / "fixtures"
GATEWAY = "http://gateway.test"


def _failure(log: str, step: str, **kw: Any) -> RoutedFailure:
    data: dict[str, Any] = json.loads((FIXTURES / "routed_failure.json").read_text("utf-8"))
    text = (FIXTURES / "logs" / log).read_text("utf-8")
    data.update(
        job_name="quality",
        failed_steps=[{"number": 5, "name": step, "conclusion": "failure"}],
        log_excerpt=text[-20_000:],
        **kw,
    )
    return RoutedFailure.model_validate(data)


class FakeKb:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, k: int, kind: str | None = None) -> list[RetrievedCase]:
        self.queries.append(f"{kind}|{query}")
        return [
            RetrievedCase(
                case_id="acme/web@aaaa..bbbb",
                similarity=0.9,
                summary="acme/web: tsc failure (TS2322)",
                fix_summary="Fixed by: fix types in api.ts; changed src/api.ts",
                source_url="https://github.com/acme/web/commit/bbbb",
            )
        ]


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUILD_LLM", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)


@pytest.mark.parametrize(
    ("log", "step", "tool"),
    [
        ("tsc_typecheck_fail.log", "Run pnpm run typecheck", "tsc"),
        ("eslint_fail.log", "Run pnpm run lint", "eslint"),
        ("vite_build_fail.log", "Run pnpm run build", "bundler"),
        ("prettier_fail.log", "Run pnpm run format:check", "other"),
    ],
)
def test_build_failures_are_diagnosed(log: str, step: str, tool: str) -> None:
    d = diagnose(_failure(log, step))
    assert d.status == "diagnosed"
    assert isinstance(d.details, BuildDetails) and d.details.tool == tool
    assert d.evidence and d.root_cause and d.remediation and d.remediation.steps
    assert d.retrieved_cases == [] and d.remediation.cites_case_ids == []


@pytest.mark.parametrize(
    ("log", "step", "category"),
    [
        ("vitest_test_fail.log", "Run pnpm exec vitest run", FailureCategory.TEST),
        ("dependency_eresolve.log", "Run npm ci", FailureCategory.DEPENDENCY),
    ],
)
def test_other_failures_are_misrouted(log: str, step: str, category: FailureCategory) -> None:
    d = diagnose(_failure(log, step))
    assert d.status == "misrouted" and d.suggested_category == category
    assert d.details is None
    assert "Cleaning up orphan processes" not in d.evidence[0].text
    assert not d.evidence[0].text.startswith("2026-")  # timestamp removed


def test_retrieved_cases_are_cited() -> None:
    kb = FakeKb()
    d = diagnose(_failure("tsc_typecheck_fail.log", "Run pnpm run typecheck"), kb=kb)
    assert [c.case_id for c in d.retrieved_cases] == ["acme/web@aaaa..bbbb"]
    assert d.remediation is not None and d.remediation.cites_case_ids == ["acme/web@aaaa..bbbb"]
    assert kb.queries[0].startswith("tsc|tsc ") and "TS2322" in kb.queries[0]


def test_a_failing_kb_does_not_stop_the_diagnosis() -> None:
    class Broken:
        def search(self, query: str, k: int, kind: str | None = None) -> list[RetrievedCase]:
            raise RuntimeError("db down")

    d = diagnose(_failure("tsc_typecheck_fail.log", "Run pnpm run typecheck"), kb=Broken())
    assert d.status == "diagnosed" and d.retrieved_cases == []


def test_full_log_is_read_from_shared_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (FIXTURES / "logs" / "eslint_fail.log").read_text("utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "1.log.gz").write_bytes(gzip.compress(text.encode()))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    failure = _failure("tsc_typecheck_fail.log", "Run pnpm run lint", log_ref="logs/1.log.gz")
    d = diagnose(failure)
    assert isinstance(d.details, BuildDetails) and d.details.tool == "eslint"


def _llm_answer(cites: list[str]) -> dict[str, Any]:
    return {
        "output": {
            "root_cause": "The new strict option makes two assignments in src/api.ts invalid.",
            "summary": "Fix the two type errors in src/api.ts.",
            "steps": ["Run pnpm run typecheck", "Fix src/api.ts"],
            "cites_case_ids": cites,
        },
        "model": "mock-1",
        "prompt_version": llm.PROMPT_VERSION,
        "cache_hit": False,
        "input_tokens": 10,
        "output_tokens": 10,
        "cost_usd": "0",
    }


def test_llm_rewrite_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUILD_LLM", "on")
    monkeypatch.setenv("LLM_GATEWAY_URL", GATEWAY)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GATEWAY}/complete").mock(
            return_value=httpx.Response(200, json=_llm_answer(["acme/web@aaaa..bbbb"]))
        )
        d = diagnose(_failure("tsc_typecheck_fail.log", "Run pnpm run typecheck"), kb=FakeKb())
    sent = json.loads(route.calls.last.request.content)
    assert sent["expert"] == "build" and sent["prompt_version"] == llm.PROMPT_VERSION
    assert d.root_cause is not None and d.root_cause.startswith("The new strict option")
    assert d.llm_model == "mock-1" and d.prompt_version == llm.PROMPT_VERSION
    assert d.remediation is not None and d.remediation.cites_case_ids == ["acme/web@aaaa..bbbb"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=_llm_answer(["made-up-case"])),
        httpx.Response(502, json={"error": "invalid_model_output"}),
        httpx.Response(200, json={"output": {"root_cause": "x"}}),
    ],
)
def test_llm_problems_keep_the_written_diagnosis(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    monkeypatch.setenv("BUILD_LLM", "on")
    monkeypatch.setenv("LLM_GATEWAY_URL", GATEWAY)
    with respx.mock() as mock:
        mock.post(f"{GATEWAY}/complete").mock(return_value=response)
        d = diagnose(_failure("tsc_typecheck_fail.log", "Run pnpm run typecheck"), kb=FakeKb())
    assert d.status == "diagnosed" and d.llm_model is None
    assert d.root_cause is not None and "TypeScript" in d.root_cause
