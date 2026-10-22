"""Worker tests for the infra expert (job function called directly; no Redis)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from infra_expert.config import EXPERT, QUEUE_NAME
from infra_expert.worker import process_failure

from shared.contracts import Diagnosis, InfraDetails

FIXTURES = Path(__file__).parent / "fixtures"
BACKEND = "http://backend.test"


def _failure_json() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "routed_failure.json").read_text("utf-8"))
    return data


def test_queue_name_comes_from_the_expert_enum() -> None:
    assert QUEUE_NAME == f"diagnose:{EXPERT.value}"
    assert QUEUE_NAME == "diagnose:infra"  # pinned: the backend enqueues to this name


def test_job_returns_valid_diagnosis_when_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKEND_URL", raising=False)
    diagnosis = process_failure(_failure_json())
    assert isinstance(diagnosis, Diagnosis)
    assert str(diagnosis.failure_id) == "22222222-2222-4222-8222-222222222222"
    assert diagnosis.expert == EXPERT
    assert diagnosis.status == "diagnosed"
    assert diagnosis.retrieved_cases == []
    assert len(diagnosis.evidence) == 1
    assert diagnosis.evidence[0].source == "log"
    assert diagnosis.evidence[0].text == "npm ERR! Found: react@18.2.0"
    assert diagnosis.details is not None
    assert diagnosis.details.expert == EXPERT.value == "infra"
    assert isinstance(diagnosis.details, InfraDetails)


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
