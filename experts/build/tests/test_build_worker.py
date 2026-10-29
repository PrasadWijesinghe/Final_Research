"""Worker tests for the build expert (job function called directly; no Redis)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from build_expert.config import EXPERT, QUEUE_NAME
from build_expert.worker import process_failure

from shared.contracts import BuildDetails, Diagnosis, FailureCategory

FIXTURES = Path(__file__).parent / "fixtures"
BACKEND = "http://backend.test"


def _failure_json() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "routed_failure.json").read_text("utf-8"))
    return data


@pytest.fixture(autouse=True)
def _no_kb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker tests run without a database: no knowledge base."""
    monkeypatch.setattr("build_expert.worker.default_kb", lambda: None)


def test_queue_name_comes_from_the_expert_enum() -> None:
    assert QUEUE_NAME == f"diagnose:{EXPERT.value}"
    assert QUEUE_NAME == "diagnose:build"  # pinned: the backend enqueues to this name


def test_job_returns_valid_diagnosis_when_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture is an npm ERESOLVE failure: not a build failure, so the expert says so.
    monkeypatch.delenv("BACKEND_URL", raising=False)
    diagnosis = process_failure(_failure_json())
    assert isinstance(diagnosis, Diagnosis)
    assert str(diagnosis.failure_id) == "22222222-2222-4222-8222-222222222222"
    assert diagnosis.expert == EXPERT
    assert diagnosis.status == "misrouted"
    assert diagnosis.suggested_category == FailureCategory.DEPENDENCY
    assert diagnosis.retrieved_cases == []
    assert diagnosis.evidence[0].source == "log"
    assert diagnosis.evidence[0].text == "npm ERR! code ERESOLVE"  # the first problem line


def test_job_diagnoses_a_typescript_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKEND_URL", raising=False)
    log = (FIXTURES / "logs" / "tsc_typecheck_fail.log").read_text("utf-8")
    payload = {
        **_failure_json(),
        "job_name": "quality",
        "failed_steps": [{"number": 5, "name": "Run pnpm run typecheck", "conclusion": "failure"}],
        "log_excerpt": log[-20_000:],
    }
    diagnosis = process_failure(payload)
    assert diagnosis.status == "diagnosed"
    assert isinstance(diagnosis.details, BuildDetails)
    assert diagnosis.details.expert == EXPERT.value == "build"
    assert diagnosis.details.tool == "tsc"
    assert diagnosis.details.locations and (diagnosis.details.locations[0].code or "").startswith(
        "TS"
    )
    assert diagnosis.root_cause and "TypeScript" in diagnosis.root_cause
    assert diagnosis.confidence == 0.9


def test_job_posts_diagnosis_to_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_URL", BACKEND + "/")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BACKEND}/diagnoses").mock(return_value=httpx.Response(201))
        returned = process_failure(_failure_json())
    body = Diagnosis.model_validate_json(route.calls.last.request.content)
    assert body.diagnosis_id == returned.diagnosis_id
    assert body.failure_id == returned.failure_id


def test_backend_error_fails_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_URL", BACKEND)
    with respx.mock() as mock:
        mock.post(f"{BACKEND}/diagnoses").mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            process_failure(_failure_json())


def test_job_rejects_invalid_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKEND_URL", raising=False)
    with pytest.raises(ValueError):
        process_failure({"failure_id": "not-a-uuid"})


def test_empty_log_excerpt_falls_back_to_step_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKEND_URL", raising=False)
    diagnosis = process_failure({**_failure_json(), "log_excerpt": ""})
    assert diagnosis.evidence[0].text == "failed step: Run npm test"
